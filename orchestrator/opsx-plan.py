#!/usr/bin/env python3
"""opsx-plan: deterministic plan-level orchestrator for OpenSpec changes.

Iterates a TOML plan manifest of OpenSpec changes (a DAG). Owns the
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
import tempfile
import time
import uuid
from datetime import datetime, timezone
from pathlib import Path

# Resolve bundled runtime modules before considering the host repository. The
# global installer places these under ~/.local/lib/opsx-controller.
_SCRIPT_ROOT = Path(__file__).resolve().parents[1]
_RUNTIME_ROOTS = (_SCRIPT_ROOT, _SCRIPT_ROOT / "lib" / "opsx-controller")


def _ensure_runtime_modules() -> None:
    for runtime_root in _RUNTIME_ROOTS:
        if (runtime_root / "lib" / "metrics").is_dir():
            runtime_root_str = str(runtime_root)
            if runtime_root_str not in sys.path:
                sys.path.insert(0, runtime_root_str)
            return


_ensure_runtime_modules()

try:
    import tomllib
except ModuleNotFoundError:  # pragma: no cover
    sys.exit("opsx-plan requires Python 3.11+ (tomllib)")

try:
    from lib.models.resolver import USER_CONFIG_PATH, ModelConfigError
    from lib.models.resolver import resolve as resolve_models
    from lib.models.resolver import validate as validate_models
    from lib.models.types import ROLE_ENV, ROLE_VARIANT_ENV, ROLES, ALL_ROLES
except ModuleNotFoundError as exc:  # pragma: no cover
    sys.exit(f"opsx-plan requires the lib.models runtime package: {exc}")

try:
    from lib.orchestrator import base, compiler, dashboard, delivery, doctor, groundtruth, logs, planref, report, telemetry
    from lib.orchestrator import cost as cost_mod
    from lib.orchestrator import state as state_mod
except ModuleNotFoundError as exc:  # pragma: no cover
    sys.exit(f"opsx-plan requires the lib.orchestrator runtime package: {exc}")
base._RUNTIME_ROOTS = _RUNTIME_ROOTS

ANSI_ESCAPE_RE = re.compile(r"\x1b\[[0-9;?]*[ -/]*[@-~]")

_current_proc: subprocess.Popen | None = None


def _build_notification_payload(
    event_type: str,
    plan_name: str,
    summary: str,
    change_id: str | None = None,
) -> str:
    """Build a JSON notification payload conforming to the stable event schema.

    For change-specific events, the payload includes ``change_id``.
    For plan-wide events (e.g. ``plan_complete``), ``change_id`` is omitted
    rather than inventing one.

    Field contract:
      - ``event_type``:  a string naming the event
      - ``plan_name``:   the resolved plan name
      - ``timestamp``:   orchestrator-generated event timestamp (UTC ISO-8601)
      - ``summary``:     short human-readable description of the event
      - ``change_id``:   present only for change-specific events
    """
    payload: dict = {
        "event_type": event_type,
        "plan_name": plan_name,
        "timestamp": base.utcnow(),
        "summary": summary,
    }
    if change_id:
        payload["change_id"] = change_id
    return json.dumps(payload, ensure_ascii=False)


def _try_notify(
    cfg: dict,
    event_type: str,
    summary: str,
    change_id: str | None = None,
) -> None:
    """Invoke ``plan.notify_cmd`` as a best-effort side effect.

    **Never raises.**  Notification-command failures are logged for operator
    triage but never change stage verdicts, plan-state transitions, or overall
    run exit semantics.

    When ``notify_cmd`` is absent (empty or unset), this function is a no-op
    and the orchestrator behaves exactly as it did before run-event
    notifications were introduced.
    """
    notify_cmd = cfg.get("notify_cmd", "").strip()
    if not notify_cmd:
        return

    plan_name = cfg["name"]
    payload_json = _build_notification_payload(
        event_type=event_type,
        plan_name=plan_name,
        summary=summary,
        change_id=change_id,
    )

    try:
        cmd_parts = shlex.split(notify_cmd)
        cmd = cmd_parts + [payload_json]
        base.log(f"  notify: {event_type} -> {notify_cmd}")

        proc = subprocess.run(
            cmd,
            capture_output=True,
            text=True,
            timeout=30,
        )
        if proc.returncode != 0:
            stderr_tail = (proc.stderr or "").strip().splitlines()[-3:]
            detail = "; " + " | ".join(stderr_tail) if stderr_tail else ""
            base.log(
                f"  notify failed ({event_type}): "
                f"exit={proc.returncode}{detail}"
            )
    except subprocess.TimeoutExpired:
        base.log(f"  notify timed out ({event_type}): {notify_cmd}")
    except FileNotFoundError:
        base.log(f"  notify command not found ({event_type}): {notify_cmd}")
    except Exception as exc:
        base.log(f"  notify error ({event_type}): {exc}")


# ---------------------------------------------------------------------------
# Plan manifest
# ---------------------------------------------------------------------------


def build_single_change_config(repo: Path, change_id: str) -> dict:
    """Build a minimal one-change direct-execution config, pinned to OpenCode.

    Synthesizes a config dict that mirrors the output of ``load_plan`` for
    exactly one already-authored OpenSpec change, without requiring a TOML
    manifest.  Always uses ``ADAPTER_DEFAULTS["opencode"]``; there is no
    ``--adapter`` flag on ``run-one`` to select ``claude-code`` here. Fails
    early when the change dir is missing or unauthored.
    """
    cdir = groundtruth.change_dir(repo, change_id)
    if not cdir.is_dir():
        raise base.PlanError(f"openspec/changes/{change_id} does not exist")
    if not groundtruth.change_authored(repo, change_id):
        raise base.PlanError(
            f"openspec/changes/{change_id} is missing required artifacts "
            f"({', '.join(groundtruth.AUTHORED_ARTIFACTS)})"
        )

    defaults = base.ADAPTER_DEFAULTS["opencode"]
    plan_name = f"run-{change_id}"

    cfg = {
        "name": plan_name,
        "adapter": "opencode",
        "state_file": defaults["state_file"],
        "implement_invoke": defaults["implement_invoke"],
        "review_invoke": defaults["review_invoke"],
        "archive_invoke": defaults["archive_invoke"],
        "timeout_minutes": 90,
        "max_rounds": 5,
        "no_progress_limit": 2,
        "fast_checks": [],
        "check_timeout_minutes": 15,
        "require_clean_tracked": True,
        "escalate_after_review_fails": 0,
        "finding_recurrence_limit": 0,
        "invalid_output_retries": 2,
        "skip_warning": False,
        "skip_suggestion": False,
        "notify_cmd": "",
        "plan_doc": "",
        "create_invoke": "",
        "create_timeout_minutes": 30,
        "create_max_attempts": 2,
        "review_created": False,
        "created_check": "openspec validate {change} --strict",
        "git_delivery": planref._parse_git_delivery_config({}),
    }

    by_id = {
        change_id: {
            "id": change_id,
            "phase": None,
            "depends_on": [],
            "pause_before": False,
            "enabled": True,
            "timeout_minutes": 90,
            "create_invoke": "",
            "create_max_attempts": 2,
        }
    }

    cfg["order"] = [change_id]
    cfg["changes"] = by_id

    try:
        cfg["models"] = resolve_models("opencode", repo=repo)
    except ModelConfigError as exc:
        raise base.PlanError(str(exc)) from exc
    apply_model_env(cfg)

    return cfg


# ---------------------------------------------------------------------------
# Single-change manifest serialization
# ---------------------------------------------------------------------------

def render_single_change_manifest(cfg: dict) -> str:
    """Serialize a single-change config to a TOML manifest string.

    Emits one ``[plan]`` table and one ``[[changes]]`` table.  Reuses the
    existing ``_escape_toml_value`` helper.
    """
    lines: list[str] = []
    lines.append("[plan]")

    # Plan-level string fields.
    plan_str_fields = {
        "name": cfg.get("name", ""),
        "adapter": cfg.get("adapter", "opencode"),
        "state_file": cfg.get("state_file", ""),
        "implement_invoke": cfg.get("implement_invoke", ""),
        "review_invoke": cfg.get("review_invoke", ""),
        "archive_invoke": cfg.get("archive_invoke", ""),
        "notify_cmd": cfg.get("notify_cmd", ""),
        "plan_doc": cfg.get("plan_doc", ""),
        "create_invoke": cfg.get("create_invoke", ""),
        "created_check": cfg.get("created_check", ""),
    }
    for key, val in plan_str_fields.items():
        lines.append(f'{key} = "{compiler._escape_toml_value(val)}"')

    # Numeric plan-level fields — use float/int to match load_plan coercion.
    lines.append(f"timeout_minutes = {float(cfg.get('timeout_minutes', 90))}")
    lines.append(f"max_rounds = {int(cfg.get('max_rounds', 5))}")
    lines.append(f"no_progress_limit = {int(cfg.get('no_progress_limit', 2))}")
    lines.append(f"escalate_after_review_fails = {int(cfg.get('escalate_after_review_fails', 0))}")
    lines.append(f"finding_recurrence_limit = {int(cfg.get('finding_recurrence_limit', 0))}")
    lines.append(f"invalid_output_retries = {int(cfg.get('invalid_output_retries', 2))}")
    lines.append(f"check_timeout_minutes = {float(cfg.get('check_timeout_minutes', 15))}")
    lines.append(f"create_timeout_minutes = {float(cfg.get('create_timeout_minutes', 30))}")
    lines.append(f"create_max_attempts = {int(cfg.get('create_max_attempts', 2))}")

    # Boolean plan-level fields.
    lines.append(f"require_clean_tracked = {_toml_bool(cfg.get('require_clean_tracked', True))}")
    lines.append(f"review_created = {_toml_bool(cfg.get('review_created', False))}")
    lines.append(f"skip_warning = {_toml_bool(cfg.get('skip_warning', False))}")
    lines.append(f"skip_suggestion = {_toml_bool(cfg.get('skip_suggestion', False))}")

    # fast_checks.
    fast_checks = cfg.get("fast_checks", [])
    if fast_checks:
        items = ", ".join(f'"{compiler._escape_toml_value(c)}"' for c in fast_checks)
        lines.append(f"fast_checks = [{items}]")
    else:
        lines.append("fast_checks = []")

    # git_delivery inline table.
    gd = cfg.get("git_delivery", {})
    lines.append(
        f"git_delivery = {{ enabled = {_toml_bool(gd.get('enabled', False))}, "
        f'branch = "{compiler._escape_toml_value(gd.get("branch", ""))}", '
        f'base_ref = "{compiler._escape_toml_value(gd.get("base_ref", ""))}", '
        f"create_pull_request = {_toml_bool(gd.get('create_pull_request', False))} }}"
    )

    # One [[changes]] entry.
    lines.append("")
    changes = cfg.get("changes", {})
    for cid, c in changes.items():
        lines.append("[[changes]]")
        lines.append(f'id = "{compiler._escape_toml_value(cid)}"')
        phase = c.get("phase")
        if phase is not None:
            lines.append(f"phase = {int(phase)}")
        depends_on = c.get("depends_on", [])
        if depends_on:
            items = ", ".join(f'"{compiler._escape_toml_value(d)}"' for d in depends_on)
            lines.append(f"depends_on = [{items}]")
        else:
            lines.append("depends_on = []")
        lines.append(f"pause_before = {_toml_bool(c.get('pause_before', False))}")
        lines.append(f"enabled = {_toml_bool(c.get('enabled', True))}")
        lines.append(f"timeout_minutes = {float(c.get('timeout_minutes', cfg.get('timeout_minutes', 90)))}")
        lines.append(f'create_invoke = "{compiler._escape_toml_value(c.get("create_invoke", ""))}"')
        lines.append(f"create_max_attempts = {int(c.get('create_max_attempts', cfg.get('create_max_attempts', 2)))}")
        break  # single change only

    return "\n".join(lines) + "\n"


def _toml_bool(value) -> str:
    """Return ``"true"`` or ``"false"`` for a Python truthy/falsy."""
    return "true" if value else "false"


def write_single_change_manifest(repo: Path, change_id: str, cfg: dict) -> None:
    """Write the derived single-change manifest, verified by round-trip.

    Writes to a temp sibling, loads it through ``load_plan``, compares the
    reloaded config against *cfg*, and only then ``os.replace``\\s it into
    position.  Raises ``PlanError`` on divergence.
    """
    ensure_opsx_plan_dir(repo)
    manifest_path = planref.single_change_manifest_path(repo, change_id)
    manifest_path.parent.mkdir(parents=True, exist_ok=True)

    toml_text = render_single_change_manifest(cfg)
    tmp_path = manifest_path.with_suffix(manifest_path.suffix + ".tmp")

    try:
        tmp_path.write_text(toml_text, encoding="utf-8")
    except OSError as exc:
        raise base.PlanError(f"could not stage derived manifest: {exc}") from exc

    # Round-trip: load it back through load_plan.
    try:
        loaded = planref.load_plan(tmp_path, repo=repo)
    except base.PlanError:
        tmp_path.unlink(missing_ok=True)
        raise
    except Exception as exc:
        tmp_path.unlink(missing_ok=True)
        raise base.PlanError(
            f"derived manifest failed to load: {exc}"
        ) from exc

    # Compare serialized fields between synthesized and reloaded configs.
    _compare_configs(cfg, loaded, tmp_path, manifest_path)


def _compare_configs(
    original: dict, loaded: dict, tmp_path: Path, manifest_path: Path,
) -> None:
    """Compare serialized fields; os.replace on success, PlanError + unlink on divergence."""
    diverging: list[str] = []

    _SERIALIZED_PLAN_KEYS = [
        "name", "adapter", "state_file",
        "implement_invoke", "review_invoke", "archive_invoke",
        "timeout_minutes", "max_rounds", "no_progress_limit",
        "escalate_after_review_fails", "finding_recurrence_limit",
        "invalid_output_retries",
        "fast_checks", "check_timeout_minutes", "require_clean_tracked",
        "skip_warning", "skip_suggestion",
        "notify_cmd", "plan_doc", "create_invoke",
        "create_timeout_minutes", "create_max_attempts",
        "review_created", "created_check", "git_delivery",
    ]

    for key in _SERIALIZED_PLAN_KEYS:
        orig_val = original.get(key)
        loaded_val = loaded.get(key)
        if not _values_equal(orig_val, loaded_val):
            diverging.append(key)

    _SERIALIZED_CHANGE_KEYS = [
        "id", "phase", "depends_on", "pause_before", "enabled",
        "timeout_minutes", "create_invoke", "create_max_attempts",
    ]

    orig_changes = original.get("changes", {})
    loaded_changes = loaded.get("changes", {})
    for cid in orig_changes:
        if cid not in loaded_changes:
            diverging.append(f"changes.{cid} (missing from loaded)")
            continue
        for field in _SERIALIZED_CHANGE_KEYS:
            orig_val = orig_changes[cid].get(field)
            loaded_val = loaded_changes[cid].get(field)
            if not _values_equal(orig_val, loaded_val):
                diverging.append(f"changes.{cid}.{field}")

    for cid in loaded_changes:
        if cid not in orig_changes:
            diverging.append(f"changes.{cid} (unexpected in loaded)")

    if diverging:
        tmp_path.unlink(missing_ok=True)
        manifest_path.unlink(missing_ok=True)
        raise base.PlanError(
            f"round-trip divergence in derived manifest: "
            f"{', '.join(diverging)}"
        )

    os.replace(tmp_path, manifest_path)


def _values_equal(a, b) -> bool:
    """Compare two values, treating equal numeric values as matching."""
    if a == b:
        return True
    # Handle numeric coercion: int 90 vs float 90.0
    if isinstance(a, (int, float)) and isinstance(b, (int, float)):
        return float(a) == float(b)
    # Handle list comparison / dict comparison
    if isinstance(a, list) and isinstance(b, list):
        return a == b
    if isinstance(a, dict) and isinstance(b, dict):
        return a == b
    return False


def apply_model_env(cfg: dict) -> None:
    """Export ``cfg["models"]`` into ``os.environ`` for the process lifetime.

    Resolution happens once per process (``opsx-plan`` handles exactly one
    plan per invocation), so no save/restore is needed: everything
    downstream — direct stage dispatch and the telemetry fallback that
    re-expands the stage invoke string after a stage completes — reads the
    same ``os.environ`` values for the rest of the process.

    Raises ``PlanError`` naming every unresolved required role rather than
    letting a worker dispatch with an empty or defaulted model.  Optional
    roles are exported only when resolved; an unresolved optional role does
    not block activation on its own.
    """
    models: dict = cfg.get("models") or {}
    unresolved = [role for role in ROLES if not (models.get(role) and models[role].model)]
    if unresolved:
        raise base.PlanError(
            f"cannot activate models for adapter '{cfg.get('adapter', '?')}': "
            f"unresolved role(s): {', '.join(unresolved)}\n"
            f"Run `opsx-plan models show --adapter {cfg.get('adapter', '?')}` to "
            f"inspect resolution, or `opsx-plan models init` to seed a "
            f"configuration file."
        )

    # Fail-closed gate: when escalation is enabled, the role must resolve.
    escalation_role = "implementer_escalation"
    escalate_threshold = cfg.get("escalate_after_review_fails", 0)
    escalation_entry = models.get(escalation_role)
    if escalate_threshold > 0 and not (escalation_entry and escalation_entry.model):
        raise base.PlanError(
            f"escalate_after_review_fails is {escalate_threshold} but "
            f"the '{escalation_role}' role is unresolved for adapter "
            f"'{cfg.get('adapter', '?')}'.\n"
            f"Run `opsx-plan models show --adapter {cfg.get('adapter', '?')}` to "
            f"inspect resolution, or `opsx-plan models init` to seed a "
            f"configuration file."
        )

    for role in ROLES:
        os.environ[ROLE_ENV[role]] = models[role].model

    # Export the optional escalation role only when resolved.
    # When unresolved, explicitly unset the variable so a previously-set
    # value from an earlier apply_model_env call does not leak into a
    # non-escalation dispatch.
    if escalation_entry and escalation_entry.model:
        os.environ[ROLE_ENV[escalation_role]] = escalation_entry.model
    else:
        os.environ.pop(ROLE_ENV[escalation_role], None)

    # Reasoning variants are optional per role. Export the resolved variant
    # (if any) for every role; an unresolved variant is set to an empty
    # string so ``--variant "$OPSX_<ROLE>_VARIANT"`` in a stage invoke drops
    # the flag instead of aborting on an unset variable.
    for role in ALL_ROLES:
        entry = models.get(role)
        os.environ[ROLE_VARIANT_ENV[role]] = entry.variant if entry and entry.variant else ""

# ---------------------------------------------------------------------------
# Active plan pointer
# ---------------------------------------------------------------------------


def ensure_opsx_plan_dir(repo: Path) -> Path:
    """Ensure ``.opsx-plan/`` exists with a self-ignoring ``.gitignore``.

    Returns the resolved ``Path`` to the ``.opsx-plan/`` directory.
    """
    dot_dir = repo / ".opsx-plan"
    dot_dir.mkdir(parents=True, exist_ok=True)
    gi = dot_dir / ".gitignore"
    if not gi.exists():
        gi.write_text("*\n", encoding="utf-8")
    return dot_dir


def write_active_plan(repo: Path, plan_rel: str) -> None:
    """Write or update the active-plan pointer file.

    The pointer is stored as a single line: the repo-relative path to the
    plan TOML.  The .opsx-plan/ directory (and its .gitignore) is created
    when missing.
    """
    ensure_opsx_plan_dir(repo)
    p = planref.active_plan_pointer_path(repo)
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
        raise base.PlanError(
            f"active plan target does not exist: {plan_rel}"
        )
    # Verify it is loadable through the existing parser
    try:
        planref.load_plan(plan_path, repo=repo)
    except base.PlanError as exc:
        raise base.PlanError(f"active plan cannot be loaded: {exc}")
    return plan_path


def worker_state_path(repo: Path, plan_name: str, cid: str) -> Path:
    return repo / ".opsx-plan" / "workers" / plan_name / f"{cid}.json"


def per_change_state_path(repo: Path, cfg: dict, cid: str) -> Path:
    """Resolve the authoritative per-change v3 state file path for *cfg*.

    The ``dsh`` adapter persists its durable per-change controller state to
    the manifest ``state_file`` template (``.opsx-controller/<change>.json``)
    at the project root — the file the worker reads back via ``STATE_FILE``.
    Every other adapter keeps an internal worker-compatibility snapshot under
    ``.opsx-plan/workers/<plan>/<change>.json``. The plan-level bookkeeping
    file (``.opsx-plan/<plan>.state.json``) is a separate internal mechanism
    for every adapter and is never presented as the per-change state file.
    """
    if cfg.get("adapter") == "dsh":
        template = cfg.get("state_file") or ""
        if template:
            return repo / template.format(change=cid)
    return worker_state_path(repo, cfg["name"], cid)


def save_json(path: Path, payload: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    with open(tmp, "w", encoding="utf-8") as fh:
        json.dump(payload, fh, indent=2)
        fh.flush()
        os.fsync(fh.fileno())
    os.replace(tmp, path)


def save_worker_state(repo: Path, cfg: dict, state: dict, cid: str) -> None:
    r = state_mod.rec(state, cid)
    payload = {
        "version": 3,
        "change": cid,
        "schema": "spec-driven",
        "status": (
            "completed" if r["status"] == base.DONE else "blocked"
            if r["status"] == base.FAILED else "running"
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
    save_json(per_change_state_path(repo, cfg, cid), payload)


def persist_direct_state(repo: Path, cfg: dict, state: dict, cid: str) -> None:
    state_mod.save_state(repo, cfg["name"], state)
    save_worker_state(repo, cfg, state, cid)


def sync_direct_worker_state(repo: Path, cfg: dict, state: dict) -> None:
    for cid in cfg["order"]:
        save_worker_state(repo, cfg, state, cid)


def validate_dsh_state_files(repo: Path, cfg: dict, state: dict) -> None:
    """Fail closed when a dsh per-change state file is unusable on resume.

    The dsh worker resumes from ``STATE_FILE`` — the authoritative
    ``.opsx-controller/<change>.json``. A malformed JSON file, or one written
    for a different change, would poison the resumed run, so the controller
    stops with an actionable diagnostic before it regenerates the file.
    Plan-level bookkeeping (``.opsx-plan/<plan>.state.json``) is untouched;
    done changes are skipped because their per-change file is regenerated
    bookkeeping, not a resume source.
    """
    if cfg.get("adapter") != "dsh":
        return
    for cid in cfg["order"]:
        if not cfg["changes"][cid]["enabled"]:
            continue
        if state_mod.rec(state, cid)["phase"] == "done":
            continue
        path = per_change_state_path(repo, cfg, cid)
        if not path.exists():
            continue
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            raise base.PlanError(
                f"dsh per-change state file is not valid JSON: {path}\n"
                f"Fix or remove the broken state file before resuming. "
                f"(parse error: {exc})"
            ) from exc
        if not isinstance(payload, dict) or payload.get("change") != cid:
            found = payload.get("change") if isinstance(payload, dict) else "<not an object>"
            raise base.PlanError(
                f"dsh per-change state file {path} belongs to a different "
                f"change (expected {cid!r}, found {found!r}); "
                "remove or replace it before resuming"
            )


def single_line(value: str) -> str:
    compact = " ".join((value or "").split())
    return compact if compact else "none"


def _prior_finding_loci(r: dict, cfg: dict) -> list[str]:
    """Return the most recently completed review round's blocking-finding loci.

    Empty for a change with no completed review round yet, and empty when
    that review reported no blocking findings (or no structured findings at
    all).
    """
    blocking = _blocking_severities(cfg)
    for entry in reversed(r["history"]):
        if entry.get("phase") != "review":
            continue
        seen: set[str] = set()
        loci: list[str] = []
        for finding in entry.get("findings", []) or []:
            if finding.get("severity") not in blocking:
                continue
            for locus in finding.get("locus", []) or []:
                if locus not in seen:
                    seen.add(locus)
                    loci.append(locus)
        return loci
    return []


def build_worker_input(repo: Path, cfg: dict, state: dict, cid: str, stage: str = "") -> str:
    r = state_mod.rec(state, cid)
    state_mod.update_task_counts(repo, state, cid)
    cache = r["context_cache"]
    lines = [
        f"CHANGE: {cid}",
        f"ROUND: {r['round']}",
        f"STATE_FILE: {per_change_state_path(repo, cfg, cid)}",
        f"LATEST_FIX_PROMPT: {single_line(r['latest_fix_prompt'])}",
        f"TASK_COUNTS: {r['task_counts']['complete']}/{r['task_counts']['total']}",
        f"CONTEXT_CACHE_STATUS: {cache['status']}",
        f"CONTEXT_CACHE_VALID: {'true' if cache['valid'] else 'false'}",
        f"CONTEXT_CACHE_SUMMARY: {single_line(cache['change_summary'])}",
    ]
    if stage == "review":
        lines.append(f"PRIOR_FINDING_LOCI: {', '.join(_prior_finding_loci(r, cfg))}")
    return "\n".join(lines)


def next_stage_log_path(repo: Path, cid: str, stage: str, round_num: int) -> Path:
    log_dir = repo / ".opsx-plan" / "logs"
    log_dir.mkdir(parents=True, exist_ok=True)
    existing = sorted(log_dir.glob(f"{cid}.{stage}.r{round_num}.*.log"))
    return log_dir / f"{cid}.{stage}.r{round_num}.{len(existing) + 1}.log"


def _build_usage_sidecar_path(
    repo: Path,
    plan_name: str,
    cid: str,
    stage: str,
    round_num: int,
) -> Path:
    """Create a unique per-stage OpenCode usage sidecar path under
    ``.opsx-plan/usage/``.

    The path is unique per invocation so concurrent stages and retries never
    collide.  The caller is responsible for creating the parent directory.
    """
    uid_suffix = uuid.uuid4().hex[:12]
    return (
        repo
        / ".opsx-plan"
        / "usage"
        / plan_name
        / cid
        / f"{stage}-r{round_num}-{uid_suffix}.jsonl"
    )


def _build_usage_sidecar_env(
    plan_name: str,
    run_id: str,
    change_id: str,
    stage: str,
    round_num: int,
    sidecar_path: Path,
) -> dict[str, str]:
    """Build the OPSX_* environment dictionary for the OpenCode plugin.

    All values are str typed to match ``subprocess.Popen`` expectations.
    """
    return {
        "OPSX_USAGE_PATH": str(sidecar_path),
        "OPSX_PLAN_NAME": str(plan_name),
        "OPSX_RUN_ID": str(run_id),
        "OPSX_CHANGE_ID": str(change_id),
        "OPSX_STAGE": str(stage),
        "OPSX_ROUND": str(round_num),
    }


def record_stage_log(
    state: dict,
    cid: str,
    stage: str,
    round_num: int,
    outcome: str,
    log_path: Path,
) -> None:
    r = state_mod.rec(state, cid)
    r["last_log"] = str(log_path)
    r["last_stage"] = {
        "name": stage,
        "round": round_num,
        "outcome": outcome,
        "log_path": str(log_path),
        "updated_at": base.utcnow(),
    }

# ---------------------------------------------------------------------------
# Cost estimation for direct stage telemetry
# ---------------------------------------------------------------------------



# ---------------------------------------------------------------------------
# Spend budget helpers (plan-run-observability cost accumulation)
# ---------------------------------------------------------------------------


def compute_run_spend(repo: Path, plan_name: str, run_id: str) -> dict:
    """Read telemetry records for *run_id* and return cumulative spend info.

    Returns a dict with keys:
    - ``cumulative_spend``: float, total estimated cost from resolved records
    - ``resolved_stages``: int, count of stages with resolved cost
    - ``unresolved_stages``: int, count of stages whose cost was unresolved or
      unavailable (excluded from the numeric total)
    """
    telemetry_dir = repo / ".opsx-plan" / "telemetry"
    jsonl_path = telemetry_dir / f"{plan_name}.jsonl"

    cumulative: float = 0.0
    resolved: int = 0
    unresolved: int = 0

    if not jsonl_path.is_file():
        return {"cumulative_spend": 0.0, "resolved_stages": 0, "unresolved_stages": 0}

    try:
        for line in jsonl_path.read_text(encoding="utf-8").splitlines():
            if not line.strip():
                continue
            try:
                record = json.loads(line)
            except json.JSONDecodeError:
                continue
            if not isinstance(record, dict):
                continue
            if record.get("run_id") != run_id:
                continue
            cost = record.get("cost", {})
            cost_status = cost.get("status", "")
            if cost_status == "estimated":
                ec = cost.get("estimated_cost")
                if isinstance(ec, (int, float)):
                    cumulative += float(ec)
                    resolved += 1
                else:
                    unresolved += 1
            elif cost_status in ("unresolved", "unavailable"):
                unresolved += 1
    except OSError:
        pass

    return {
        "cumulative_spend": cumulative,
        "resolved_stages": resolved,
        "unresolved_stages": unresolved,
    }


PERMISSION_REJECTION_MARKERS = [
    "permission requested",
    "auto-rejecting",
    "The user rejected permission",
    "external_directory permission denied",
]

PROVIDER_FAILURE_MARKERS = [
    "Insufficient Balance",
    "insufficient credits",
    "quota exceeded",
    "billing hard limit",
]


def _clean_log_lines(text: str) -> list[str]:
    lines: list[str] = []
    for raw in text.splitlines():
        stripped = ANSI_ESCAPE_RE.sub("", raw).strip()
        if not stripped or stripped.startswith("# "):
            continue
        lines.append(stripped)
    return lines


def _find_last_json_object(lines: list[str]) -> dict | None:
    for candidate in reversed(lines):
        # Claude occasionally prefixes its required final JSON with a Markdown
        # inline-code backtick but omits the closing delimiter.
        if candidate.startswith("`"):
            candidate = candidate.lstrip("`").strip()
        if not (candidate.startswith("{") and candidate.endswith("}")):
            continue
        try:
            payload = json.loads(candidate)
        except json.JSONDecodeError:
            continue
        if not isinstance(payload, dict):
            continue
        return payload
    return None


def _is_claude_result_envelope(obj: dict) -> bool:
    return obj.get("type") == "result" and isinstance(obj.get("result"), str)


def _find_last_envelope(lines: list[str]) -> dict | None:
    """Return the last Claude Code result envelope object among *lines*.

    Scans forward so the *last* ``type: result`` object wins, which keeps
    ``--output-format stream-json`` (JSONL, one object per line) correct: an
    intermediate streamed message must never shadow the final result.
    """
    last_envelope: dict | None = None
    for candidate in lines:
        if candidate.startswith("`"):
            candidate = candidate.lstrip("`").strip()
        if not (candidate.startswith("{") and candidate.endswith("}")):
            continue
        try:
            obj = json.loads(candidate)
        except json.JSONDecodeError:
            continue
        if isinstance(obj, dict) and _is_claude_result_envelope(obj):
            last_envelope = obj
    return last_envelope


def _scan_for_failure_marker(lines: list[str]) -> str:
    joined = " ".join(line.lower() for line in lines)
    for marker in PERMISSION_REJECTION_MARKERS:
        if marker.lower() in joined:
            return (
                f"permission denied before JSON output "
                f"(marker: {marker!r} found in {len(lines)} lines)"
            )
    for marker in PROVIDER_FAILURE_MARKERS:
        if marker.lower() in joined:
            return (
                f"provider failure before JSON output "
                f"(marker: {marker!r} found in {len(lines)} lines)"
            )
    return ""


def parse_stage_json(log_path: Path) -> tuple[dict | None, str, dict | None]:
    """Parse the worker's final JSON object from a stage log.

    Returns ``(payload, reason, envelope)``. *envelope* is the selected
    Claude Code result envelope object when one was found in the log
    (``None`` for adapters that write worker JSON directly, e.g. OpenCode).
    """
    lines = _clean_log_lines(log_path.read_text(encoding="utf-8"))

    envelope = _find_last_envelope(lines)
    if envelope is not None:
        result_lines = _clean_log_lines(envelope.get("result", ""))
        payload = _find_last_json_object(result_lines)
        if payload is not None:
            return payload, "", envelope
        marker_reason = _scan_for_failure_marker(result_lines) or _scan_for_failure_marker(lines)
        if marker_reason:
            return None, marker_reason, envelope
        return None, (
            f"expected a final JSON object line, got {len(result_lines)} non-comment lines"
        ), envelope

    payload = _find_last_json_object(lines)
    if payload is not None:
        return payload, "", None
    marker_reason = _scan_for_failure_marker(lines)
    if marker_reason:
        return None, marker_reason, None
    return None, f"expected a final JSON object line, got {len(lines)} non-comment lines", None


# Appended to the worker input when a stage is retried after producing no
# usable result envelope.  The hint restates the output contract without
# prescribing stage-specific content.
_INVALID_OUTPUT_RETRY_HINT = (
    "RETRY_CORRECTION: the previous attempt at this stage ended without the "
    "required machine-readable result. Re-run the stage from scratch. Your "
    "final message must be exactly one line containing a single JSON object "
    "in the required shape — no prose, summary, markdown, or code fences "
    "before or after it."
)


def _is_retriable_invalid_output(parse_why: str) -> bool:
    """Return True when a parse failure is worth retrying in-place.

    Generic "no final JSON" failures — model contract misses, truncated
    streams, transient provider 5xx pages — may succeed on a fresh attempt.
    Permission rejections and billing/quota provider failures are named by
    their marker reason and stay terminal: retrying them never helps.
    """
    return parse_why.startswith("expected a final JSON object line")


def record_archive_evidence(repo: Path, record: dict, cid: str) -> bool:
    archive_dir = groundtruth.find_archive_dir(repo, cid)
    if archive_dir is None:
        return False
    commit = groundtruth.find_archive_commit(repo, cid)
    if not commit and not groundtruth.archive_dir_ignored(repo):
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
    state_mod.rec(state, cid)["history"].append(entry)


def reachable_commit(repo: Path, commit: str) -> bool:
    if not commit:
        return False
    res = groundtruth.git(repo, "merge-base", "--is-ancestor", commit, "HEAD")
    return res.returncode == 0


def resolve_commit(repo: Path, commit: str) -> str:
    if not commit:
        return ""
    res = groundtruth.git(repo, "rev-parse", "--verify", commit)
    return res.stdout.strip() if res.returncode == 0 else ""


def verify_direct_archive_done(repo: Path, cid: str, record: dict) -> tuple[bool, str]:
    archive = record["archive"]
    if archive.get("status") != "passed":
        return False, "no fresh archive worker result recorded"
    if groundtruth.change_dir(repo, cid).exists():
        return False, f"openspec/changes/{cid} still exists"
    archive_path = archive.get("path", "")
    if not archive_path:
        return False, "archive worker did not record archive path"
    archive_dir = repo / archive_path
    if not archive_dir.is_dir():
        return False, f"archive path missing: {archive_path}"
    actual_archive = groundtruth.find_archive_dir(repo, cid)
    if actual_archive is None:
        return False, "no dated archive directory found"
    if actual_archive.resolve() != archive_dir.resolve():
        return False, (
            f"archive directory mismatch: expected {archive_path}, found "
            f"{actual_archive.relative_to(repo)}"
        )
    # Whether the `archive(<id>):` commit is required evidence depends on the
    # repo: when openspec/changes/archive/ is gitignored the archive worker has
    # nothing to stage and legitimately produces no commit, so it degrades to a
    # corroborating signal. When the directory is tracked it stays load-bearing
    # — a missing commit there means the archive was never durably recorded.
    commit_optional = groundtruth.archive_dir_ignored(repo)
    commit = archive.get("commit", "")
    if not commit:
        if not commit_optional:
            return False, "archive worker did not record archive commit"
        base.log(
            f"  note: {cid} archived with no archive(<id>): commit "
            f"(archive directory is gitignored)"
        )
    elif not reachable_commit(repo, commit):
        if not commit_optional:
            return False, f"archive commit not reachable from HEAD: {commit}"
        base.log(f"  note: {cid} archive commit not reachable from HEAD: {commit}")
    else:
        resolved_commit = resolve_commit(repo, commit)
        if not resolved_commit and not commit_optional:
            return False, f"archive commit could not be resolved: {commit}"
        latest_commit = groundtruth.find_archive_commit(repo, cid)
        if latest_commit and resolved_commit and latest_commit != resolved_commit:
            base.log(
                f"  note: {cid} archive state recorded {resolved_commit[:12]} but "
                f"newer archive(<change>) commit {latest_commit[:12]} is reachable"
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


# ---------------------------------------------------------------------------
# Finding locus normalization (recurrence detection)
# ---------------------------------------------------------------------------

_TRACKED_FILES_CACHE: dict[str, list[str]] = {}


def tracked_files(repo: Path) -> list[str]:
    """Return ``git ls-files`` output for *repo*, cached per repo for the run.

    Locus normalization resolves against tracked files once per finding, so
    this cache keeps the process from shelling out to git once per locus.
    """
    key = str(repo)
    if key not in _TRACKED_FILES_CACHE:
        res = groundtruth.git(repo, "ls-files")
        _TRACKED_FILES_CACHE[key] = (
            [line for line in res.stdout.splitlines() if line]
            if res.returncode == 0
            else []
        )
    return _TRACKED_FILES_CACHE[key]


_LOCUS_WRAP_CHARS = " \t\r\n`"
_LOCUS_TRAILING_PUNCT = ".,;:!?)]}\"'`"


def _resolve_locus_path(path: str, files: list[str]) -> str:
    """Resolve *path* to the one tracked file it is a unique suffix of.

    Returns *path* unchanged when it matches no tracked file, or matches more
    than one (an ambiguous suffix) — an unresolvable or ambiguous locus is
    still retained, in trimmed form, so it can participate in comparison.
    """
    if not path:
        return path
    matches = [f for f in files if f == path or f.endswith("/" + path)]
    return matches[0] if len(matches) == 1 else path


def normalize_finding_locus(raw: str, files: list[str]) -> str:
    """Normalize one reviewer-reported locus string for identity comparison.

    Trims surrounding whitespace, backticks, and trailing punctuation,
    converts path separators to POSIX form, and splits the optional
    ``:<symbol>`` suffix. The path portion is resolved against *files* (see
    ``_resolve_locus_path``); the symbol portion, if present, is compared
    exactly and is not itself normalized.
    """
    text = (raw or "").strip(_LOCUS_WRAP_CHARS)
    text = text.rstrip(_LOCUS_TRAILING_PUNCT)
    text = text.replace("\\", "/")
    if ":" in text:
        path_part, _, symbol_part = text.rpartition(":")
    else:
        path_part, symbol_part = text, ""
    resolved = _resolve_locus_path(path_part.strip(), files)
    return f"{resolved}:{symbol_part}" if symbol_part else resolved


def normalize_finding_loci(finding: dict, files: list[str]) -> list[str]:
    """Return the normalized, de-duplicated, order-preserving loci for one finding."""
    raw_loci = finding.get("locus", [])
    if not isinstance(raw_loci, list):
        return []
    seen: set[str] = set()
    normalized: list[str] = []
    for entry in raw_loci:
        if not isinstance(entry, str) or not entry.strip():
            continue
        norm = normalize_finding_locus(entry, files)
        if norm and norm not in seen:
            seen.add(norm)
            normalized.append(norm)
    return normalized


_ENV_VAR_RE = re.compile(r"\$(?:\{(\w+)\}|(\w+))")


def _expand_invoke_token(token: str) -> tuple[str | None, str]:
    """Expand ``$VAR``/``${VAR}`` references in *token*.

    Returns ``(expanded, "")`` on success. When a referenced variable is
    unset, returns ``(None, var_name)`` naming the first such variable. When
    a referenced variable is set to an empty string (or the token contains
    no variable at all and expands to empty), returns ``("", "")`` so the
    caller can drop the token and any dangling flag that precedes it.
    """
    if "$" not in token:
        return token, ""
    expanded = os.path.expandvars(token)
    unresolved = _ENV_VAR_RE.search(expanded)
    if unresolved:
        # Reference survived expansion: the variable is entirely unset.
        return None, unresolved.group(1) or unresolved.group(2)
    if not expanded:
        # Fully expanded to empty: the referenced variable was set to an
        # empty string (e.g. an optional reasoning variant). Expand to
        # empty and let the caller omit the flag.
        return "", ""
    return expanded, ""


def invoke_direct_stage(
    repo: Path,
    cfg: dict,
    cid: str,
    stage: str,
    round_num: int,
    input_block: str,
) -> tuple[str, Path]:
    tokens = shlex.split(cfg[f"{stage}_invoke"])
    expanded_tokens: list[str] = []
    for token in tokens:
        value, missing_var = _expand_invoke_token(token)
        if value is None:
            message = (
                f"stage invoke references unset environment variable "
                f"'{missing_var}'"
            )
            log_path = next_stage_log_path(repo, cid, stage, round_num)
            log_path.write_text(f"# {base.utcnow()} {stage}: {message}\n", encoding="utf-8")
            base.log(f"  exec[{stage}]: aborted - {message}")
            return "env_error", log_path
        expanded_tokens.append(value)

    # Drop tokens that expanded to empty (a set-but-empty variable, e.g. an
    # optional reasoning variant) along with a preceding flag token that
    # would otherwise dangle as ``--variant ""``. Required model variables
    # are never empty because apply_model_env fails closed on unresolved
    # roles.
    cmd: list[str] = []
    for token in expanded_tokens:
        if not token:
            if cmd and cmd[-1].startswith("-") and "=" not in cmd[-1]:
                cmd.pop()
            continue
        cmd.append(token)

    cmd = cmd + [input_block]
    log_path = next_stage_log_path(repo, cid, stage, round_num)
    timeout_s = cfg["changes"][cid]["timeout_minutes"] * 60
    base.log(
        f"  exec[{stage}]: {' '.join(cmd[:-1])} <input> "
        f"(timeout {timeout_s / 60:g}m, log {log_path})"
    )
    return run_logged_command(repo, cmd, log_path, timeout_s, stage, round_num, input_text=input_block)


def run_logged_command(
    repo: Path,
    cmd: list[str],
    log_path: Path,
    timeout_s: float,
    stage: str,
    attempt: int,
    input_text: str = "",
) -> tuple[str, Path]:
    global _current_proc
    header_cmd = cmd
    if cmd and "\n" in cmd[-1]:
        # The trailing argument is a multi-line worker input block; elide it
        # from the header the same way exec[stage] does, so its raw text
        # (which carries no JSON but may contain phrases that look like
        # failure markers) never lands in the log as ordinary lines.
        header_cmd = cmd[:-1] + ["<input>"]
    try:
        with open(log_path, "w", encoding="utf-8") as lf:
            lf.write(f"# {base.utcnow()} {stage} attempt {attempt}: {' '.join(header_cmd)}\n")
            # Write the worker input block as comment-prefixed metadata so
            # operators can inspect the exact dispatched fields (including
            # corrective handoffs) while the existing JSON/failure-marker
            # parser in _clean_log_lines ignores `# `-prefixed lines.
            if input_text:
                lf.write("# --- OPSX WORKER INPUT ---\n")
                for line in input_text.splitlines():
                    stripped = line.strip()
                    if stripped:
                        lf.write(f"# {stripped}\n")
                lf.write("# --- END OPSX WORKER INPUT ---\n")
            lf.flush()
            proc = subprocess.Popen(
                cmd,
                cwd=repo,
                stdout=lf,
                stderr=subprocess.STDOUT,
                start_new_session=True,
                env=os.environ.copy(),
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
    r = state_mod.rec(state, cid)
    status = payload.get("status")
    if status == "blocked":
        r["last_result"] = "implement_blocked"
        state_mod.update_task_counts(repo, state, cid)
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
        state_mod.set_status(state, cid, base.FAILED, payload.get("reason", "implement blocked"))
        _try_notify(cfg, "change_failed", payload.get("summary", "change blocked"), change_id=cid)
        return "stop"
    if status != "implemented":
        state_mod.set_status(state, cid, base.FAILED, f"implement returned unexpected status={status}")
        r["last_result"] = "implement_invalid"
        _try_notify(cfg, "change_failed", f"implement returned unexpected status={status}", change_id=cid)
        return "stop"
    r["task_counts"] = normalize_task_counts(payload)
    progress = bool(payload.get("progress_made"))
    r["no_progress_streak"] = 0 if progress else r["no_progress_streak"] + 1
    files_touched = [str(path) for path in payload.get("files_touched", [])]
    known_change_files = [str(path) for path in payload.get("known_change_files", [])]
    r["tracked_change_files"] = state_mod.merge_paths(
        state_mod.change_context_paths(repo, cid),
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
        state_mod.set_status(state, cid, base.FAILED, "no progress ceiling reached")
        _try_notify(cfg, "change_failed", "no progress ceiling reached", change_id=cid)
        return "stop"
    # Completeness gate: `implemented` means every automatable task is
    # checked in the tasks file (ground truth, not the worker's advisory
    # remaining_tasks). Unchecked automatable tasks re-enter implement with a
    # controller-generated corrective prompt naming them, consuming the
    # change's normal round budget; only when every remaining task is manual
    # does the change advance to review.
    remaining = state_mod.remaining_automatable_tasks(repo, cid)
    if remaining:
        task_ids = ", ".join(remaining)
        task_locus = f"openspec/changes/{cid}/tasks.md"
        r["latest_fix_prompt"] = (
            f"CHANGE: {cid}\n"
            f"FINDINGS:\n"
            f"- [critical] {task_locus}: these automatable tasks are still "
            f"unchecked: {task_ids}\n"
            f"  → complete them and mark each task line complete in tasks.md\n"
            f"CORRECTIVE GUIDANCE: Finish the remaining automatable work for "
            f"the change and check each task in {task_locus} "
            f"(- [ ] → - [x]). Tasks whose line ends in (manual) are "
            f"operator-only and may stay unchecked.\n"
            f"VERIFY: reread {task_locus} and confirm every non-(manual) task "
            f"is checked before reporting implemented."
        )
        append_history(
            state,
            cid,
            {
                "round": r["round"],
                "phase": "implement",
                "status": "incomplete",
                "summary": f"implemented with automatable tasks remaining: {task_ids}",
                "remaining_tasks": remaining,
            },
        )
        if r["round"] >= r["max_rounds"]:
            r["last_result"] = "max_rounds_reached"
            reason = f"implement retry budget exhausted; automatable tasks still unchecked: {task_ids}"
            state_mod.set_status(state, cid, base.FAILED, reason)
            _try_notify(cfg, "change_failed", reason, change_id=cid)
            return "stop"
        r["last_result"] = "implement_incomplete"
        r["round"] += 1
        r["phase"] = "implement"
        state_mod.set_status(state, cid, base.PENDING, f"automatable tasks remaining: {task_ids}")
        return "continue"
    r["phase"] = "review"
    state_mod.set_status(state, cid, base.PENDING, payload.get("summary", "implementation complete"))
    return "continue"


_VALID_FINDING_SEVERITIES = {"critical", "warning", "note"}


def normalize_review_findings(payload: dict, files: list[str]) -> list[dict]:
    """Extract and normalize the reviewer's ``findings`` array for persistence.

    Tolerates a missing or malformed ``findings`` array (returns ``[]``,
    contributing no recurrence evidence) so legacy review payloads keep
    driving the loop exactly as before. Individual malformed entries within
    an otherwise valid list are skipped rather than discarding the round.
    """
    raw_findings = payload.get("findings")
    if not isinstance(raw_findings, list):
        return []
    normalized: list[dict] = []
    for finding in raw_findings:
        if not isinstance(finding, dict):
            continue
        severity = finding.get("severity")
        if severity not in _VALID_FINDING_SEVERITIES:
            continue
        locus = normalize_finding_loci(finding, files)
        if not locus:
            continue
        normalized.append(
            {
                "severity": severity,
                "locus": locus,
                "statement": str(finding.get("statement", "")),
            }
        )
    return normalized


def _blocking_severities(cfg: dict) -> set[str]:
    """Return the finding severities that gate the review verdict.

    Mirrors the ``skip_warning``/``skip_suggestion`` gate in
    ``apply_review_result`` so recurrence composes with it rather than
    duplicating separate logic.
    """
    skip_warning = cfg.get("skip_warning", False)
    skip_suggestion = cfg.get("skip_suggestion", False) or skip_warning
    severities = {"critical"}
    if not skip_warning:
        severities.add("warning")
    if not skip_suggestion:
        severities.add("note")
    return severities


def _locus_recurrence_rounds(history: list[dict], blocking: set[str]) -> dict[str, set[int]]:
    """Map each normalized locus to the distinct review rounds that cited it.

    Only findings whose severity is in *blocking* contribute. Multiple
    blocking findings citing the same locus within one round contribute that
    round once, since the map value is a set.
    """
    locus_rounds: dict[str, set[int]] = {}
    for entry in history:
        if entry.get("phase") != "review":
            continue
        round_num = entry.get("round")
        for finding in entry.get("findings", []) or []:
            if finding.get("severity") not in blocking:
                continue
            for locus in finding.get("locus", []) or []:
                locus_rounds.setdefault(locus, set()).add(round_num)
    return locus_rounds


def apply_review_result(repo: Path, cfg: dict, state: dict, cid: str, payload: dict) -> str:
    r = state_mod.rec(state, cid)
    if payload.get("status") != "reviewed":
        state_mod.set_status(
            state,
            cid,
            base.FAILED,
            f"review returned unexpected status={payload.get('status')}",
        )
        r["last_result"] = "review_invalid"
        _try_notify(cfg, "change_failed", f"review returned unexpected status={payload.get('status')}", change_id=cid)
        return "stop"
    counts = normalize_finding_counts(payload)
    verdict = payload.get("verdict")
    summary = payload.get("summary", "review completed")
    fix_prompt = payload.get("fix_prompt", "")
    state_mod.update_task_counts(repo, state, cid)
    findings = normalize_review_findings(payload, tracked_files(repo))
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
            "findings": findings,
        },
    )
    if verdict not in {"pass", "fail"}:
        state_mod.set_status(state, cid, base.FAILED, f"review returned unexpected verdict={verdict}")
        r["last_result"] = "review_invalid"
        _try_notify(cfg, "change_failed", f"review returned unexpected verdict={verdict}", change_id=cid)
        return "stop"
    # Review workers apply a strict rule and recommend `fail` for any non-zero
    # count, so the skip keys cannot work by deferring to the verdict. They are
    # operator policy the controller applies on top of that recommendation:
    # count only the severities that still gate, and accept a recommended
    # failure whose remaining findings are all skipped. With no skip configured
    # the verdict still has to agree, preserving the strict default gate.
    skip_warning = cfg.get("skip_warning", False)
    skip_suggestion = cfg.get("skip_suggestion", False) or skip_warning
    blocking = counts["critical"]
    if not skip_warning:
        blocking += counts["warning"]
    if not skip_suggestion:
        blocking += counts["note"]
    if skip_warning or skip_suggestion:
        passed = blocking == 0
    else:
        passed = verdict == "pass" and blocking == 0
    if passed:
        r["latest_fix_prompt"] = ""
        r["last_result"] = "review_passed"
        r["phase"] = "archive"
        state_mod.set_status(state, cid, base.PENDING, summary)
        return "continue"
    r["latest_fix_prompt"] = fix_prompt
    recurrence_limit = cfg.get("finding_recurrence_limit", 0)
    if recurrence_limit > 0:
        locus_rounds = _locus_recurrence_rounds(r["history"], _blocking_severities(cfg))
        for locus, rounds in locus_rounds.items():
            if len(rounds) >= recurrence_limit:
                rounds_desc = ", ".join(str(n) for n in sorted(rounds))
                reason = (
                    f"finding recurrence ceiling reached: locus '{locus}' cited by a "
                    f"blocking finding in rounds {rounds_desc}"
                )
                r["last_result"] = "finding_recurrence_exceeded"
                state_mod.set_status(state, cid, base.FAILED, reason)
                _try_notify(cfg, "change_failed", reason, change_id=cid)
                return "stop"
    if r["round"] >= r["max_rounds"]:
        r["last_result"] = "max_rounds_reached"
        state_mod.set_status(state, cid, base.FAILED, "review retry budget exhausted")
        _try_notify(cfg, "change_failed", "review retry budget exhausted", change_id=cid)
        return "stop"
    r["last_result"] = "review_failed"
    r["round"] += 1
    r["phase"] = "implement"
    state_mod.set_status(state, cid, base.PENDING, summary)
    return "continue"


def apply_archive_result(repo: Path, cfg: dict, state: dict, cid: str, payload: dict) -> str:
    r = state_mod.rec(state, cid)
    archive = r["archive"]
    if payload.get("status") == "blocked":
        archive.update(
            {
                "status": "failed",
                "path": payload.get("archive_path", ""),
                "commit": payload.get("commit", ""),
                "reason": payload.get("reason", "archive blocked"),
                "spec_sync_status": payload.get("spec_sync_status", "not_started"),
                "triage": payload.get("triage", state_mod.default_archive_state()["triage"]),
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
        state_mod.set_status(state, cid, base.FAILED, payload.get("reason", "archive blocked"))
        _try_notify(cfg, "change_failed", payload.get("summary", "archive blocked"), change_id=cid)
        return "stop"
    if payload.get("status") != "archived":
        state_mod.set_status(
            state,
            cid,
            base.FAILED,
            f"archive returned unexpected status={payload.get('status')}",
        )
        archive["status"] = "failed"
        archive["reason"] = "invalid archive output"
        r["last_result"] = "archive_invalid"
        _try_notify(cfg, "change_failed", f"archive returned unexpected status={payload.get('status')}", change_id=cid)
        return "stop"
    archive.update(
        {
            "status": "passed",
            "path": payload.get("archive_path", ""),
            "commit": payload.get("commit", ""),
            "reason": "",
            "spec_sync_status": payload.get("spec_sync_status", ""),
            "triage": state_mod.default_archive_state()["triage"],
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
            "manual_tasks_pending": state_mod.pending_manual_tasks(repo, cid),
        },
    )
    r["last_result"] = "archive_passed"
    ok, why = verify_direct_archive_done(repo, cid, r)
    if not ok:
        archive["status"] = "failed"
        archive["reason"] = why
        state_mod.set_status(state, cid, base.FAILED, f"archive unverified: {why}")
        _try_notify(cfg, "change_failed", f"archive unverified: {why}", change_id=cid)
        return "stop"
    checks_ok, check_why = groundtruth.run_fast_checks(repo, cfg)
    if not checks_ok:
        archive["status"] = "failed"
        archive["reason"] = f"post-archive {check_why}"
        r["last_result"] = "post_archive_check_failed"
        state_mod.set_status(state, cid, base.FAILED, f"post-archive {check_why}")
        _try_notify(cfg, "change_failed", f"post-archive {check_why}", change_id=cid)
        return "stop"
    clean_ok, clean_why = delivery.verify_post_archive_clean(repo, cfg)
    if not clean_ok:
        archive["status"] = "failed"
        archive["reason"] = f"post-archive {clean_why}"
        r["last_result"] = "post_archive_dirty_tracked"
        state_mod.set_status(state, cid, base.FAILED, f"post-archive {clean_why}")
        _try_notify(cfg, "change_failed", f"post-archive {clean_why}", change_id=cid)
        return "stop"
    r["phase"] = "done"
    # After the archive move, tasks.md lives in the archive directory; parse
    # it there for the operator's post-archive manual checklist.
    r["manual_tasks_pending"] = state_mod.pending_manual_tasks(repo, cid)
    state_mod.set_status(state, cid, base.DONE, "verified + checks passed")
    _try_notify(cfg, "change_done", f"change {cid} completed", change_id=cid)
    return "done"


def _escalation_active_for_dispatch(cfg: dict, r: dict) -> bool:
    """Return True when escalation should be active for the next implement dispatch.

    Escalation is active when the threshold is > 0 and
    (round - 1) >= threshold, i.e. the number of failed reviews has
    reached the threshold.
    """
    threshold = cfg.get("escalate_after_review_fails", 0)
    if threshold <= 0:
        return False
    return (r["round"] - 1) >= threshold


def run_direct_change(
    repo: Path,
    cfg: dict,
    state: dict,
    cid: str,
    budget_deadline: float | None = None,
    budget_usd: float = 0.0,
) -> str:
    r = state_mod.rec(state, cid)
    while True:
        if budget_deadline and time.monotonic() > budget_deadline:
            state_mod.set_status(state, cid, base.PENDING, f"budget exhausted while waiting to run {r['phase']}")
            persist_direct_state(repo, cfg, state, cid)
            return "budget"
        stage = r["phase"]
        round_num = r["round"]
        if stage == "done":
            ok, why = verify_direct_archive_done(repo, cid, r)
            if ok:
                state_mod.set_status(state, cid, base.DONE, "verified + checks passed")
            else:
                state_mod.set_status(state, cid, base.FAILED, f"completed state no longer verifiable: {why}")
            persist_direct_state(repo, cfg, state, cid)
            return r["status"]
        # --- spend-budget pre-dispatch check ---
        if budget_usd > 0:
            run_id_for_check = state.get("run_id", "")
            if run_id_for_check:
                spend = compute_run_spend(repo, cfg["name"], run_id_for_check)
                if spend["cumulative_spend"] >= budget_usd:
                    reason = (
                        f"spend budget exhausted: "
                        f"${spend['cumulative_spend']:.2f} >= ${budget_usd:.2f} "
                        f"({spend['resolved_stages']} stages resolved, "
                        f"{spend['unresolved_stages']} unresolved)"
                    )
                    r["last_result"] = "spend_budget_exhausted"
                    state_mod.set_status(state, cid, base.PENDING, reason)
                    base.log(f"  {reason}")
                    persist_direct_state(repo, cfg, state, cid)
                    return "budget"
        if stage not in {"implement", "review", "archive"}:
            r["phase"] = "implement"
            stage = "implement"

        input_block = build_worker_input(repo, cfg, state, cid, stage=stage)
        state_mod.set_status(state, cid, base.RUNNING, f"{stage} round {round_num}")
        persist_direct_state(repo, cfg, state, cid)

        # 3.1 Capture started_at before invocation
        started_at = base.utcnow()
        plan_name = cfg["name"]
        run_id = telemetry.get_or_create_run_id(repo, cfg, state)

        # ---- usage sidecar (OpenCode plugin only; harmless no-op for other adapters) ----
        sidecar_path: Path | None = None
        extra_env: dict[str, str] | None = None
        saved_env: dict[str, str] = {}

        def _arm_usage_sidecar() -> None:
            """Create a fresh per-attempt sidecar and export its OPSX_* env."""
            nonlocal sidecar_path, extra_env
            if not (plan_name and run_id):
                return
            sidecar_path = _build_usage_sidecar_path(repo, plan_name, cid, stage, round_num)
            sidecar_path.parent.mkdir(parents=True, exist_ok=True)
            extra_env = _build_usage_sidecar_env(plan_name, run_id, cid, stage, round_num, sidecar_path)
            for key, value in extra_env.items():
                if key not in saved_env:
                    saved_env[key] = os.environ.get(key, "")
                os.environ[key] = value

        def _restore_usage_sidecar() -> None:
            if not extra_env:
                return
            for key in extra_env:
                os.environ.pop(key, None)
                if key in saved_env:
                    os.environ[key] = saved_env[key]

        _arm_usage_sidecar()

        def _write_telemetry(telemetry_status: str, error_message: str | None) -> None:
            """Write a telemetry record. Logs a warning on failure; never raises."""
            try:
                telemetry._record_stage_telemetry(
                    repo, cfg, state, cid, stage, round_num,
                    started_at, ended_at, duration_ms,
                    telemetry_status, error_message,
                    payload, log_path,
                    sidecar_path=sidecar_path,
                    envelope=envelope,
                )
            except Exception as exc:
                base.log(f"warning: failed to write telemetry for {cid}/{stage} r{round_num}: {exc}")

        # ---- escalation: swap OPSX_IMPLEMENTER_MODEL before each implement dispatch ----
        if stage == "implement":
            impl_env_key = ROLE_ENV["implementer"]
            # Prefer the cfg-resolved base model (the immutable source of truth
            # set by apply_model_env) so a prior change's escalation cannot leak
            # into an un-escalated dispatch.  Fall back to the current env value
            # for callers that pass a cfg dict without a resolved models entry.
            models = cfg.get("models", {})
            impl_entry = models.get("implementer")
            if impl_entry and impl_entry.model:
                base_model = impl_entry.model
            else:
                base_model = os.environ.get(impl_env_key, "")
            active = _escalation_active_for_dispatch(cfg, r)
            esc_model = os.environ.get(ROLE_ENV["implementer_escalation"], "")
            if active and esc_model:
                os.environ[impl_env_key] = esc_model
                r["escalation"] = {
                    "active": True,
                    "activated_round": r["escalation"]["activated_round"] or round_num,
                    "model": esc_model,
                }
            else:
                os.environ[impl_env_key] = base_model
                if not r["escalation"]["active"]:
                    r["escalation"] = {
                        "active": False,
                        "activated_round": 0,
                        "model": "",
                    }

        # ---- stage dispatch with bounded retry on invalid worker output ----
        # A worker that finishes but never emits its final JSON envelope
        # (model contract miss, transient provider 5xx, truncated stream)
        # used to fail the whole change on the spot, discarding the work it
        # did.  Generic parse failures are retried in-place up to
        # ``invalid_output_retries`` times with a corrective hint appended to
        # the worker input.  Permission rejections and billing/quota provider
        # failures stay terminal — retrying those never helps.
        invalid_retries_max = max(0, int(cfg.get("invalid_output_retries", 2)))
        invalid_attempt = 0
        attempt_input = input_block
        payload: dict | None = None
        parse_why = ""
        envelope: dict | None = None
        while True:
            outcome, log_path = invoke_direct_stage(repo, cfg, cid, stage, round_num, attempt_input)

            # ---- restore os.environ after subprocess invocation ----
            _restore_usage_sidecar()
            record_stage_log(state, cid, stage, round_num, outcome, log_path)

            # 3.2 Capture ended_at, compute duration, determine telemetry status
            ended_at = base.utcnow()
            duration_ms = telemetry.compute_duration_ms(started_at, ended_at)
            payload = None
            parse_why = ""
            envelope = None

            if outcome == "env_error":
                reason = log_path.read_text(encoding="utf-8").splitlines()[0].split(": ", 1)[-1]
                _write_telemetry("spawn_error", reason)
                state_mod.rec(state, cid)["last_result"] = f"{stage}_env_error"
                state_mod.set_status(state, cid, base.FAILED, reason)
                _try_notify(cfg, "change_failed", reason, change_id=cid)
                persist_direct_state(repo, cfg, state, cid)
                return "spawn_error"

            if outcome == "spawn_error":
                _write_telemetry(
                    "spawn_error",
                    f"could not spawn {stage}: {cfg[f'{stage}_invoke']}",
                )
                state_mod.rec(state, cid)["last_result"] = f"{stage}_spawn_error"
                state_mod.set_status(state, cid, base.FAILED, f"could not spawn {stage}: {cfg[f'{stage}_invoke']}")
                _try_notify(cfg, "change_failed", f"could not spawn {stage}", change_id=cid)
                persist_direct_state(repo, cfg, state, cid)
                return "spawn_error"

            if outcome == "timeout":
                _write_telemetry("timeout", f"{stage} timed out")
                state_mod.rec(state, cid)["last_result"] = f"{stage}_timeout"
                state_mod.set_status(state, cid, base.FAILED, f"{stage} timed out")
                _try_notify(cfg, "change_failed", f"{stage} timed out", change_id=cid)
                persist_direct_state(repo, cfg, state, cid)
                return "failed"

            payload, parse_why, envelope = parse_stage_json(log_path)
            if payload is not None:
                break
            if _is_retriable_invalid_output(parse_why) and invalid_attempt < invalid_retries_max:
                invalid_attempt += 1
                _write_telemetry("invalid_output", parse_why)
                base.log(
                    f"  {stage} round {round_num}: output invalid ({parse_why}); "
                    f"retrying ({invalid_attempt}/{invalid_retries_max})"
                )
                # Re-arm a fresh usage sidecar for the retry attempt.
                _arm_usage_sidecar()
                attempt_input = input_block + "\n" + _INVALID_OUTPUT_RETRY_HINT
                continue
            _write_telemetry("invalid_output", parse_why)
            state_mod.rec(state, cid)["last_result"] = "subagent_output_invalid"
            if stage == "archive":
                state_mod.rec(state, cid)["archive"]["status"] = "failed"
                state_mod.rec(state, cid)["archive"]["reason"] = parse_why
            state_mod.set_status(state, cid, base.FAILED, f"{stage} output invalid: {parse_why}")
            _try_notify(cfg, "change_failed", f"{stage} output invalid", change_id=cid)
            persist_direct_state(repo, cfg, state, cid)
            return "failed"

        # Parseable payload: apply control-flow dispatch first, then record
        # telemetry with the definitive outcome.
        if stage == "implement":
            action = apply_implement_result(repo, cfg, state, cid, payload)
        elif stage == "review":
            action = apply_review_result(repo, cfg, state, cid, payload)
        else:
            action = apply_archive_result(repo, cfg, state, cid, payload)
        persist_direct_state(repo, cfg, state, cid)

        # Determine telemetry status from the control-flow decision.
        if action == "stop":
            telemetry_status = "failed"
            last_result = state_mod.rec(state, cid).get("last_result", "")
            reason = state_mod.rec(state, cid).get("reason", "")
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
# Doctor / preflight checks
# ---------------------------------------------------------------------------


def _diff_orchestrator_package(repo_pkg: Path, installed_pkg: Path) -> str:
    """Compare the installed lib.orchestrator tree against the repo copy.

    Returns a non-empty reason string when a module differs, is missing, or
    exists only in the installed copy; returns "" when they match.
    """
    import hashlib

    if not installed_pkg.is_dir():
        return "Installed lib.orchestrator runtime package is missing; rerun a global installer"

    def hashes(root: Path) -> dict[str, bytes]:
        return {
            str(p.relative_to(root)): hashlib.sha256(p.read_bytes()).digest()
            for p in sorted(root.rglob("*.py"))
        }

    try:
        repo_hashes = hashes(repo_pkg)
        installed_hashes = hashes(installed_pkg)
    except OSError:
        return ""

    if repo_hashes.keys() != installed_hashes.keys() or any(
        repo_hashes[name] != installed_hashes[name] for name in repo_hashes
    ):
        return "Installed lib.orchestrator runtime package is stale; rerun a global installer"
    return ""


def _check_stale_install(repo: Path) -> tuple[bool, str, str]:
    """Check that installed ~/.local/bin/opsx-plan and its lib.orchestrator
    runtime package match the repo copy by content hash."""
    import hashlib

    label = "Installed orchestrator matches repo copy"
    installed = Path.home() / ".local" / "bin" / "opsx-plan"
    repo_copy = repo / "orchestrator" / "opsx-plan.py"

    if not installed.is_file():
        return (False, label, "Installed opsx-plan not found at ~/.local/bin/opsx-plan; run the installer")
    if not repo_copy.is_file():
        return (True, label, "")

    try:
        if hashlib.sha256(repo_copy.read_bytes()).digest() != hashlib.sha256(installed.read_bytes()).digest():
            return (False, label, "Installed copy is stale; rerun the installer")
    except OSError:
        return (True, label, "")

    repo_pkg = repo / "lib" / "orchestrator"
    if repo_pkg.is_dir():
        installed_pkg = Path.home() / ".local" / "lib" / "opsx-controller" / "lib" / "orchestrator"
        stale_reason = _diff_orchestrator_package(repo_pkg, installed_pkg)
        if stale_reason:
            return (False, label, stale_reason)

    return (True, label, "")


def run_doctor_checks(repo: Path, plan_src: str | None,
                      adapter: str = "opencode", cfg: dict | None = None) -> int:
    """Run all doctor preflight checks. Returns count of failures."""
    checks: list[tuple[bool, str, str]] = []

    # Plan-independent checks
    checks.append(_check_stale_install(repo))
    checks.append(doctor._check_model_resolution(repo, adapter))
    checks.append(doctor._check_model_identifier_syntax(repo, adapter))
    checks.append(doctor._check_openspec_on_path())
    checks.append(doctor._check_openspec_initialized(repo))
    checks.append(doctor._check_adapter_client_on_path(adapter))
    checks.append(doctor._check_tracked_bytecode(repo))
    checks.append(doctor._check_tracked_tree_clean(repo))

    # Plan-dependent checks
    checks.append(doctor._check_plan_loads(repo, plan_src))
    checks.append(doctor._check_pr_delivery(repo, plan_src))
    checks.append(doctor._check_direct_worker_agents(cfg, repo))

    failures = 0
    for passed, label, remediation in checks:
        if passed:
            print(f"  \u2713 {label}")
        else:
            print(f"  \u2717 {label}")
            if remediation:
                print(f"    \u2192 {remediation}")
            failures += 1
        if label == "Model roles resolve for the target adapter":
            doctor._print_model_resolution_detail(repo, adapter)

    return failures


def run_preflight_warnings(repo: Path, plan_src: str | None,
                           adapter: str = "opencode", cfg: dict | None = None) -> None:
    """Run the same checks as doctor but emit warnings without changing outcome."""
    checks: list[tuple[bool, str, str]] = []

    checks.append(_check_stale_install(repo))
    checks.append(doctor._check_model_resolution(repo, adapter))
    checks.append(doctor._check_model_identifier_syntax(repo, adapter))
    checks.append(doctor._check_openspec_on_path())
    checks.append(doctor._check_openspec_initialized(repo))
    checks.append(doctor._check_adapter_client_on_path(adapter))
    checks.append(doctor._check_tracked_bytecode(repo))
    checks.append(doctor._check_tracked_tree_clean(repo))
    checks.append(doctor._check_plan_loads(repo, plan_src))
    checks.append(doctor._check_pr_delivery(repo, plan_src))
    checks.append(doctor._check_direct_worker_agents(cfg, repo))

    for passed, label, remediation in checks:
        if not passed:
            detail = f"{label}: {remediation}" if remediation else label
            base.log(f"  \u26a0 {detail}")


# ---------------------------------------------------------------------------
# Drive invocation
# ---------------------------------------------------------------------------

def run_stage(
    repo: Path, cfg: dict, cid: str, stage: str, invoke_tpl: str,
    timeout_minutes: float, attempt: int,
) -> tuple[str, Path]:
    """Run a templated stage command ('create'). Returns
    (outcome, log_path) where outcome is 'exited', 'timeout', or
    'spawn_error'. Output goes to a log file so it can be tailed live;
    the exit code is informational only."""
    log_dir = repo / ".opsx-plan" / "logs"
    log_dir.mkdir(parents=True, exist_ok=True)
    log_path = log_dir / f"{cid}.{stage}{attempt}.log"

    cmd = shlex.split(
        invoke_tpl.format(change=cid, plan_doc=cfg["plan_doc"],
                          controller_model=os.environ.get("OPSX_CONTROLLER_MODEL", ""))
    )
    timeout_s = timeout_minutes * 60
    base.log(f"  exec[{stage}]: {' '.join(cmd)}  "
        f"(timeout {timeout_s/60:g}m, log {log_path})")
    return run_logged_command(repo, cmd, log_path, timeout_s, stage, attempt)


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
    base.log("interrupted; terminating active stage process group")
    if _current_proc is not None:
        terminate_group(_current_proc)
    sys.exit(130)


# ---------------------------------------------------------------------------
# Scheduling
# ---------------------------------------------------------------------------

def classify(cfg: dict, state: dict, cid: str) -> str:
    """Computed status for reporting: includes blocked/awaiting_approval."""
    c = cfg["changes"][cid]
    r = state_mod.rec(state, cid)
    if not c["enabled"]:
        return base.SKIPPED
    if r["status"] in (base.DONE, base.FAILED, base.RUNNING):
        return r["status"]
    for dep in c["depends_on"]:
        dep_status = classify(cfg, state, dep)
        if dep_status in (base.FAILED, "blocked"):
            return "blocked"
        if dep_status != base.DONE:
            return base.PENDING
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
        r = state_mod.rec(state, cid)
        archived_on_disk = (
            not groundtruth.change_dir(repo, cid).exists() and groundtruth.find_archive_dir(repo, cid) is not None
        )
        r["max_rounds"] = cfg["max_rounds"]
        if r["status"] == base.RUNNING:  # stale from a killed run
            state_mod.set_status(state, cid, base.PENDING, "recovered from interrupted run")
        # A change that failed only because no create_invoke was configured
        # (so create never ran: create_attempts == 0) should re-queue once the
        # operator supplies one — otherwise the stale reason keeps reporting
        # "no create_invoke configured" even after the plan is fixed, and the
        # operator has to guess that a manual `reset` is required.
        if (
            r["status"] == base.FAILED
            and r.get("create_attempts", 0) == 0
            and not groundtruth.change_authored(repo, cid)
            and not archived_on_disk
            and cfg["changes"][cid]["create_invoke"]
        ):
            state_mod.set_status(state, cid, base.PENDING, "create_invoke now configured; will retry")
            base.log(f"reconcile: {cid} create config now present; re-queued")
            continue
        if r["status"] != base.DONE:
            if archived_on_disk and record_archive_evidence(repo, r, cid):
                ok, why = verify_direct_archive_done(repo, cid, r)
                if ok:
                    r["phase"] = "done"
                    state_mod.set_status(
                        state,
                        cid,
                        base.DONE,
                        "verified from repository archive evidence",
                    )
                    base.log(f"reconcile: {cid} already archived; marked done")
                    continue
                r["archive"]["status"] = "failed"
                r["archive"]["reason"] = why
            if r["archive"].get("status") == "passed":
                ok, why = verify_direct_archive_done(repo, cid, r)
                if ok:
                    r["phase"] = "done"
                    state_mod.set_status(
                        state,
                        cid,
                        base.DONE,
                        "verified from plan state + repository evidence",
                    )
                    base.log(f"reconcile: {cid} already archived; marked done")
                    continue
                if archived_on_disk:
                    state_mod.set_status(
                        state,
                        cid,
                        base.FAILED,
                        f"recorded archive success but evidence is inconsistent: {why}",
                    )
                    base.log(f"reconcile: {cid} archive evidence inconsistent: {why}")
                    continue
            elif archived_on_disk:
                state_mod.set_status(
                    state,
                    cid,
                    base.FAILED,
                    "repository archived change but plan state lacks archive worker evidence",
                )
                base.log(
                    f"reconcile: {cid} archived on disk without plan-owned archive evidence"
                )
                continue
            if (
                r["status"] == base.PENDING
                and r.get("create_attempts", 0) > 0
                and groundtruth.change_authored(repo, cid)
                and not r.get("created_by_orchestrator")
            ):
                created_ok, created_why = groundtruth.verify_change_created(repo, cfg, cid)
                if created_ok:
                    r["created_by_orchestrator"] = True
                    state_mod.set_status(state, cid, base.PENDING, "created and verified")
                    base.log(f"reconcile: {cid} already created; marked for acceptance")
                else:
                    state_mod.set_status(
                        state, cid, base.PENDING,
                        f"create verification pending: {created_why}",
                    )
        else:
            ok, why = verify_direct_archive_done(repo, cid, r)
            if not ok:
                state_mod.set_status(
                    state, cid, base.FAILED,
                    f"recorded done but evidence missing: {why}",
                )
                base.log(f"reconcile: {cid} done-state no longer verifiable: {why}")


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
        planref.load_plan(plan_path, repo=repo)
    except (base.PlanError, Exception) as exc:
        # tomllib.TOMLDecodeError and PlanError both indicate invalid plan
        print(f"error: invalid plan: {exc}", file=sys.stderr)
        return 2
    try:
        rel = str(plan_path.relative_to(repo))
    except ValueError:
        print(f"error: plan must be inside the repository: {plan_path}", file=sys.stderr)
        return 2
    write_active_plan(repo, rel)
    base.log(f"active plan set to: {rel}")
    print(f"Activated: {rel}")
    return 0


def cmd_run(args: argparse.Namespace) -> int:
    repo = Path(args.repo).resolve()
    plan_src = planref.resolve_plan(repo, args.plan)
    plan_abs = planref._resolve_plan_path(repo, plan_src)
    cfg = planref.load_plan(plan_abs, repo=repo)
    # The flags are additive overrides: passing one turns the skip on for this
    # run, but omitting one must not clobber a manifest that already set it.
    cfg["skip_warning"] = bool(cfg.get("skip_warning", False)) or getattr(
        args, "skip_warning", False
    )
    cfg["skip_suggestion"] = bool(cfg.get("skip_suggestion", False)) or getattr(
        args, "skip_suggestion", False
    )
    try:
        apply_model_env(cfg)
    except base.PlanError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2
    # Auto-activate when an explicit path was supplied (only after load_plan
    # succeeds to avoid rewriting the pointer on failed explicit runs).
    if args.plan:
        try:
            rel = str(plan_abs.relative_to(repo))
            write_active_plan(repo, rel)
            base.log(f"active plan set to: {rel}")
        except ValueError:
            pass  # plan outside repo — skip auto-activation
    state = state_mod.load_state(repo, cfg["name"])
    signal.signal(signal.SIGINT, handle_sigint)

    validate_dsh_state_files(repo, cfg, state)
    reconcile(repo, cfg, state)
    state_mod.save_state(repo, cfg["name"], state)
    sync_direct_worker_state(repo, cfg, state)

    # --- emit one-time notifications for awaiting states ---
    notified = state.setdefault("notified_events", {})
    for cid in cfg["order"]:
        if not cfg["changes"][cid]["enabled"]:
            continue
        status = classify(cfg, state, cid)
        change_notified = notified.setdefault(cid, [])
        if status == "awaiting_approval" and "awaiting_approval" not in change_notified:
            _try_notify(cfg, "awaiting_approval", f"change {cid} awaiting approval", change_id=cid)
            change_notified.append("awaiting_approval")
        elif status == "awaiting_acceptance" and "awaiting_acceptance" not in change_notified:
            _try_notify(cfg, "awaiting_acceptance", f"change {cid} awaiting acceptance", change_id=cid)
            change_notified.append("awaiting_acceptance")
    state_mod.save_state(repo, cfg["name"], state)

    # Run preflight checks as warnings only — never change run outcome.
    run_preflight_warnings(repo, plan_src, cfg["adapter"], cfg)

    # --- OpenSpec initialization gate ---
    # Direct-dispatch workers read their phase prompts from per-project files
    # that `openspec init` writes. An uninitialized repo would dispatch workers
    # that fail mid-run for a missing prompt file. Fail closed before any
    # dispatch with the exact command to run.
    if not args.dry_run and not getattr(args, "skip_openspec", False):
        init_ok, _, init_err = doctor._check_openspec_initialized(repo)
        if not init_ok:
            print(f"error: {init_err}", file=sys.stderr)
            print(
                "error: run `openspec init` from the repo root and rerun opsx-plan; "
                "or pass --skip-openspec to proceed without the check",
                file=sys.stderr,
            )
            return 2

    # --- git delivery: ensure delivery branch before any stage dispatch ---
    if not args.dry_run:
        no_branch = getattr(args, "no_branch", False)
        proceed, delivery_err = delivery.ensure_delivery_branch(repo, cfg, state, no_branch=no_branch)
        if not proceed:
            print(f"error: {delivery_err}", file=sys.stderr)
            return 2
        state_mod.save_state(repo, cfg["name"], state)

        # --- PR delivery preflight ---
        no_pr = getattr(args, "no_pr", False)
        if not no_pr:
            ok, preflight_err, remote_name = delivery.check_pr_delivery_prerequisites(repo, cfg)
            if not ok:
                print(f"error: {preflight_err}", file=sys.stderr)
                return 2
            if remote_name:
                state.setdefault("git_delivery", state_mod._default_git_delivery_state())
                state["git_delivery"]["remote_name"] = remote_name

    if args.dry_run:
        return cmd_status_inner(cfg, state, header="dry run: planned order")

    budget_deadline = (
        time.monotonic() + args.budget_minutes * 60 if args.budget_minutes else None
    )
    budget_usd = (
        float(args.budget_usd) if getattr(args, "budget_usd", 0) and float(args.budget_usd) > 0 else 0.0
    )
    ran = 0
    visited: set[str] = set()  # avoid re-picking the same change this run

    while True:
        if budget_deadline and time.monotonic() > budget_deadline:
            base.log("wall-clock budget exhausted; stopping")
            break
        if args.max_changes and ran >= args.max_changes:
            base.log("max-changes reached; stopping")
            break

        create_only_ok = {"ready", "awaiting_approval"} if args.create_only else {"ready"}
        ready = [
            c for c in cfg["order"]
            if c not in visited and classify(cfg, state, c) in create_only_ok
        ]
        if args.only:
            ready = [c for c in ready if c in args.only]
        if not ready:
            # --- emit one-time notifications for any change newly awaiting input ---
            for cid in cfg["order"]:
                if not cfg["changes"][cid]["enabled"]:
                    continue
                status = classify(cfg, state, cid)
                change_notified = notified.setdefault(cid, [])
                if status == "awaiting_approval" and "awaiting_approval" not in change_notified:
                    _try_notify(cfg, "awaiting_approval", f"change {cid} awaiting approval", change_id=cid)
                    change_notified.append("awaiting_approval")
                elif status == "awaiting_acceptance" and "awaiting_acceptance" not in change_notified:
                    _try_notify(cfg, "awaiting_acceptance", f"change {cid} awaiting acceptance", change_id=cid)
                    change_notified.append("awaiting_acceptance")
            state_mod.save_state(repo, cfg["name"], state)
            break

        cid = ready[0]
        change_cfg = cfg["changes"][cid]
        r = state_mod.rec(state, cid)
        needs_create = not groundtruth.change_authored(repo, cid)

        if cfg["require_clean_tracked"] and not groundtruth.tracked_tree_clean(repo):
            base.log("tracked worktree is dirty; refusing to start a new stage")
            base.log("commit/stash tracked modifications, then re-run")
            return 2

        # ----- create stage: automate the repetitive /opsx-ff invocation -----
        if needs_create:
            if not change_cfg["create_invoke"]:
                state_mod.set_status(
                    state, cid, base.FAILED,
                    "change not created and no create_invoke configured",
                )
                state_mod.save_state(repo, cfg["name"], state)
                continue
            # A previous attempt may have left a bare scaffold (just
            # .openspec.yaml). `openspec new change` refuses a populated dir, so
            # clear a pure untracked scaffold to let the author command start
            # clean; refuse if the dir holds authored or tracked content.
            if groundtruth.change_dir(repo, cid).is_dir():
                if groundtruth.scaffold_is_clearable(repo, cid):
                    shutil.rmtree(groundtruth.change_dir(repo, cid))
                    base.log(f"  removed incomplete scaffold openspec/changes/{cid}/ "
                        f"before re-create")
                else:
                    state_mod.set_status(
                        state, cid, base.FAILED,
                        f"openspec/changes/{cid} exists but is incomplete "
                        f"(missing {', '.join(groundtruth.AUTHORED_ARTIFACTS)}) and holds "
                        f"authored or tracked content; finish or remove it, "
                        f"then reset",
                    )
                    state_mod.save_state(repo, cfg["name"], state)
                    continue
            c_attempt = r["create_attempts"] + 1
            if c_attempt > change_cfg["create_max_attempts"]:
                state_mod.set_status(state, cid, base.FAILED, "create retry budget exhausted")
                state_mod.save_state(repo, cfg["name"], state)
                continue

            base.log(f"=== {cid} create "
                f"(attempt {c_attempt}/{change_cfg['create_max_attempts']}) ===")
            r["create_attempts"] = c_attempt
            state_mod.set_status(state, cid, base.RUNNING, "creating change")
            state_mod.save_state(repo, cfg["name"], state)
            before_tracked = groundtruth.tracked_worktree_snapshot(repo)

            outcome, log_path = run_stage(
                repo, cfg, cid, "create", change_cfg["create_invoke"],
                cfg["create_timeout_minutes"], c_attempt,
            )
            r["last_log"] = str(log_path)

            if outcome == "spawn_error":
                state_mod.set_status(state, cid, base.FAILED,
                           f"could not spawn create: {change_cfg['create_invoke']}")
                state_mod.save_state(repo, cfg["name"], state)
                return 2

            ok, why = groundtruth.verify_change_created(repo, cfg, cid, before_tracked)
            if ok:
                r["created_by_orchestrator"] = True
                state_mod.set_status(state, cid, base.PENDING, "created and verified")
                base.log(f"  created: {cid}")
                if cfg["review_created"]:
                    base.log(f"  awaiting acceptance — review openspec/changes/{cid}/ "
                        f"then run: opsx-plan accept <plan> {cid}")
                    change_notified = notified.setdefault(cid, [])
                    if "awaiting_acceptance" not in change_notified:
                        _try_notify(cfg, "awaiting_acceptance", f"change {cid} awaiting acceptance", change_id=cid)
                        change_notified.append("awaiting_acceptance")
            else:
                if outcome == "timeout":
                    why = f"create timed out; {why}"
                if c_attempt < change_cfg["create_max_attempts"]:
                    state_mod.set_status(state, cid, base.PENDING, f"create will retry: {why}")
                    base.log(f"  create not verified ({why}); retrying")
                else:
                    state_mod.set_status(state, cid, base.FAILED, f"create failed: {why}")
                    base.log(f"  CREATE FAILED: {why}")
            state_mod.save_state(repo, cfg["name"], state)
            # re-classify: acceptance gate may now hold this change
            continue

        if args.create_only:
            visited.add(cid)  # exists already; nothing to create, don't drive
            continue

        base.log(f"=== {cid} direct {cfg['adapter']} execution (round {r['round']}) ===")
        result = run_direct_change(repo, cfg, state, cid, budget_deadline, budget_usd)
        if result == base.DONE:
            base.log(f"  done: {cid}")
            ran += 1
        elif result == "spawn_error":
            return 2
        visited.add(cid)

    # --- PR delivery: push branch + create PR after all changes done ---
    if not args.dry_run:
        no_pr = getattr(args, "no_pr", False)
        all_done = all(
            classify(cfg, state, cid) == base.DONE
            for cid in cfg["order"]
            if cfg["changes"][cid]["enabled"]
        )
        if all_done:
            plan_notified = notified.setdefault("_plan_", [])
            if "plan_complete" not in plan_notified:
                _try_notify(cfg, "plan_complete", f"plan {cfg['name']} complete")
                plan_notified.append("plan_complete")
            ok, delivery_err = delivery.attempt_pr_delivery(repo, cfg, state, no_pr=no_pr)
            if not ok:
                print(f"error: {delivery_err}", file=sys.stderr)
                # Save state (which may include partial delivery outcome)
                # before returning an error status.
                state_mod.save_state(repo, cfg["name"], state)
                print()
                return cmd_status_inner(cfg, state, header="run finished (PR delivery failed)")
            gd_state = state.get("git_delivery", {})
            if gd_state.get("delivery_status") == "pr_opened":
                plan_notified = notified.setdefault("_plan_", [])
                if "pull_request_opened" not in plan_notified:
                    _try_notify(
                        cfg, "pull_request_opened",
                        f"pull request opened for plan {cfg['name']}: {gd_state.get('pull_request_url', '')}",
                    )
                    plan_notified.append("pull_request_opened")
            state_mod.save_state(repo, cfg["name"], state)

    print()
    return cmd_status_inner(cfg, state, header="run finished")


def cmd_status(args: argparse.Namespace) -> int:
    repo = Path(args.repo).resolve()
    plan_src = planref.resolve_plan(repo, args.plan)
    cfg = planref.load_plan(planref._resolve_plan_path(repo, plan_src), repo=repo)
    state = state_mod.load_state(repo, cfg["name"])
    validate_dsh_state_files(repo, cfg, state)
    reconcile(repo, cfg, state)
    state_mod.save_state(repo, cfg["name"], state)
    sync_direct_worker_state(repo, cfg, state)
    header = f"plan: {cfg['name']}"
    active = planref.read_active_plan(repo)
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
    # Short-form commands when the plan was resolved through the active-plan
    # flow (no explicit plan argument). Long-form when an explicit plan path
    # that differs from the active pointer is used.
    plan_arg = (
        None if args.plan is None or (active and args.plan == active)
        else plan_src
    )
    return cmd_status_inner(cfg, state, header=header, plan_arg=plan_arg)


def display_order(cfg: dict) -> list[str]:
    """Phase-ascending for human reading (P0, P1, ...), with the scheduler's
    topological order as a stable tiebreaker within a phase. Changes without a
    phase sort last. cfg['order'] itself stays topological for dispatch."""
    topo_index = {cid: i for i, cid in enumerate(cfg["order"])}

    def key(cid: str) -> tuple:
        phase = cfg["changes"][cid].get("phase")
        return (phase is None, phase if phase is not None else 0, topo_index[cid])

    return sorted(cfg["order"], key=key)


def cmd_status_inner(cfg: dict, state: dict, header: str,
                     plan_arg: str | None = None) -> int:
    print(header)
    width = max(len(c) for c in cfg["order"])
    failed = 0
    for cid in display_order(cfg):
        status = classify(cfg, state, cid)
        r = state_mod.rec(state, cid)
        extra = f"  ({r['reason']})" if r.get("reason") and status != base.DONE else ""
        phase = cfg["changes"][cid].get("phase")
        phase_s = f"P{phase} " if phase is not None else ""
        print(f"  {phase_s}{cid.ljust(width)}  {status}{extra}")
        if status in (base.FAILED, "blocked"):
            failed += 1
        # Next-command guidance for blocked changes
        if status == "awaiting_approval":
            if plan_arg:
                print(f"    \u2192 opsx-plan approve {plan_arg} {cid}")
            else:
                print(f"    \u2192 opsx-plan approve {cid}")
        elif status == "awaiting_acceptance":
            if plan_arg:
                print(f"    \u2192 opsx-plan accept {plan_arg} {cid}")
            else:
                print(f"    \u2192 opsx-plan accept {cid}")
        elif status == base.FAILED:
            if plan_arg:
                print(f"    \u2192 opsx-plan reset {plan_arg} {cid}")
            else:
                print(f"    \u2192 opsx-plan reset {cid}")
        if status == base.DONE and r.get("manual_tasks_pending"):
            print("    manual follow-up (operator checklist):")
            for task in r["manual_tasks_pending"]:
                print(f"      - {single_line(task)}")
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

    plan_path = planref.resolve_plan(repo, args.plan)
    cfg = planref.load_plan(planref._resolve_plan_path(repo, plan_path), repo=repo)
    state = state_mod.load_state(repo, cfg["name"])

    if args.approve_all:
        affected = [
            cid for cid in cfg["order"]
            if classify(cfg, state, cid) == "awaiting_approval"
        ]
        if not affected:
            print("No changes are currently awaiting approval.")
            return 0
        for cid in affected:
            if cid not in state["approvals"]:
                state["approvals"].append(cid)
                base.log(f"approved: {cid}")
        print(f"Approved: {', '.join(affected)}")
        state_mod.save_state(repo, cfg["name"], state)
        return 0

    if not args.change:
        print("error: at least one change id is required", file=sys.stderr)
        return 2
    changes = resolve_changes(cfg, args.change)
    if changes is None:
        return 2
    for cid in changes:
        if cid not in state["approvals"]:
            state["approvals"].append(cid)
            base.log(f"approved: {cid}")
    state_mod.save_state(repo, cfg["name"], state)
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

    plan_path = planref.resolve_plan(repo, args.plan)
    cfg = planref.load_plan(planref._resolve_plan_path(repo, plan_path), repo=repo)
    state = state_mod.load_state(repo, cfg["name"])

    if args.accept_all:
        affected = [
            cid for cid in cfg["order"]
            if classify(cfg, state, cid) == "awaiting_acceptance"
        ]
        if not affected:
            print("No changes are currently awaiting acceptance.")
            return 0
        had_failure = False
        accepted: list[str] = []
        for cid in affected:
            ok, why = groundtruth.verify_change_created(repo, cfg, cid)
            if not ok:
                print(f"refusing to accept {cid}: {why}", file=sys.stderr)
                had_failure = True
                continue
            state_mod.rec(state, cid)["accepted"] = True
            base.log(f"accepted: {cid}")
            accepted.append(cid)
        if accepted:
            print(f"Accepted: {', '.join(accepted)}")
            state_mod.save_state(repo, cfg["name"], state)
        return 2 if had_failure else 0

    if not args.change:
        print("error: at least one change id is required", file=sys.stderr)
        return 2
    changes = resolve_changes(cfg, args.change)
    if changes is None:
        return 2
    had_failure = False
    changed = False
    for cid in changes:
        ok, why = groundtruth.verify_change_created(repo, cfg, cid)
        if not ok:
            print(f"refusing to accept {cid}: {why}", file=sys.stderr)
            had_failure = True
            continue
        state_mod.rec(state, cid)["accepted"] = True
        base.log(f"accepted: {cid}")
        changed = True
    if changed:
        state_mod.save_state(repo, cfg["name"], state)
    return 2 if had_failure else 0


def cmd_reset(args: argparse.Namespace) -> int:
    repo = Path(args.repo).resolve()
    # Heuristic: when the first positional doesn't look like a TOML path,
    # reinterpret it as a change ID and resolve the plan.
    if args.plan is not None and not (
        args.plan.endswith(".toml") or "/" in args.plan or "\\" in args.plan
    ):
        args.change.insert(0, args.plan)
        args.plan = None

    plan_path = planref.resolve_plan(repo, args.plan)
    cfg = planref.load_plan(planref._resolve_plan_path(repo, plan_path), repo=repo)
    state = state_mod.load_state(repo, cfg["name"])

    if args.failed:
        affected = [
            cid for cid in cfg["order"]
            if classify(cfg, state, cid) == base.FAILED
        ]
        if not affected:
            print("No failed changes to reset.")
            return 0
        for cid in affected:
            state["changes"][cid] = state_mod.new_change_record()
            state["changes"][cid]["max_rounds"] = cfg["max_rounds"]
            state["changes"][cid]["reason"] = "reset by operator"
            state["changes"][cid]["updated_at"] = base.utcnow()
            base.log(f"reset: {cid}")
        print(f"Reset: {', '.join(affected)}")
        state_mod.save_state(repo, cfg["name"], state)
        return 0

    if not args.change:
        print("error: at least one change id is required", file=sys.stderr)
        return 2
    changes = resolve_changes(cfg, args.change)
    if changes is None:
        return 2
    for cid in changes:
        state["changes"][cid] = state_mod.new_change_record()
        state["changes"][cid]["max_rounds"] = cfg["max_rounds"]
        state["changes"][cid]["reason"] = "reset by operator"
        state["changes"][cid]["updated_at"] = base.utcnow()
        base.log(f"reset: {cid}")
    state_mod.save_state(repo, cfg["name"], state)
    return 0


def cmd_run_one(args: argparse.Namespace) -> int:
    """Run exactly one authored OpenSpec change through the direct OpenCode loop.

    Pinned to the OpenCode adapter via ``build_single_change_config``; there
    is no ``--adapter`` flag to select ``claude-code`` for this entry point.
    """
    repo = Path(args.repo).resolve()
    change_id = args.change

    cdir = groundtruth.change_dir(repo, change_id)
    if not cdir.is_dir():
        print(f"error: openspec/changes/{change_id} does not exist", file=sys.stderr)
        return 2
    if not groundtruth.change_authored(repo, change_id):
        print(
            f"error: openspec/changes/{change_id} is missing required artifacts "
            f"({', '.join(groundtruth.AUTHORED_ARTIFACTS)})",
            file=sys.stderr,
        )
        return 2

    cfg = build_single_change_config(repo, change_id)
    state = state_mod.load_state(repo, cfg["name"])
    signal.signal(signal.SIGINT, handle_sigint)

    if cfg["require_clean_tracked"] and not groundtruth.tracked_tree_clean(repo):
        print(
            "error: tracked worktree is dirty; commit/stash then re-run",
            file=sys.stderr,
        )
        return 2

    # Set before serialization so the derived manifest records the skips this
    # run actually applies, and so round-trip verification compares them.
    cfg["skip_warning"] = bool(cfg.get("skip_warning", False)) or getattr(
        args, "skip_warning", False
    )
    cfg["skip_suggestion"] = bool(cfg.get("skip_suggestion", False)) or getattr(
        args, "skip_suggestion", False
    )
    write_single_change_manifest(repo, change_id, cfg)

    validate_dsh_state_files(repo, cfg, state)
    reconcile(repo, cfg, state)
    state_mod.save_state(repo, cfg["name"], state)
    sync_direct_worker_state(repo, cfg, state)

    r = state_mod.rec(state, change_id)
    if r["status"] == base.DONE:
        base.log(f"{change_id} is already done")
        return 0

    base.log(f"=== {change_id} direct {cfg['adapter']} execution (round {r['round']}) ===")
    budget_usd = (
        float(args.budget_usd) if getattr(args, "budget_usd", 0) and float(args.budget_usd) > 0 else 0.0
    )
    result = run_direct_change(repo, cfg, state, change_id, budget_usd=budget_usd)

    if result == base.DONE:
        base.log(f"  done: {change_id}")
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

    manifest_rel = planref.single_change_manifest_path(repo, change_id).relative_to(repo)
    print(f"  Report:  opsx-plan report {manifest_rel}")
    print(f"           opsx-plan report --for-change {change_id}")
    print(f"  Dashboard: opsx-plan dashboard {manifest_rel}")
    print(f"             opsx-plan dashboard --for-change {change_id}")

    display = r["status"]
    if r.get("reason"):
        display += f" ({r['reason']})"
    print(f"  {change_id}  {display}")
    return 0 if result == base.DONE else 1


def cmd_compile(args: argparse.Namespace) -> int:
    """opsx-plan compile <source.md> [-o <output.toml>] [--force] [--adapter <adapter>] [--timeout-minutes <minutes>]"""
    repo = Path(args.repo).resolve()
    adapter = getattr(args, "adapter", "opencode") or "opencode"
    timeout_minutes = getattr(args, "timeout_minutes", 10.0) or 10.0

    # Reject unsupported adapters before model resolution.
    entry = compiler.COMPILE_CLIENTS.get(adapter)
    if entry is None:
        print(f"error: unknown adapter '{adapter}'; "
              f"known adapters: {', '.join(sorted(compiler.COMPILE_CLIENTS))}",
              file=sys.stderr)
        return 2
    if not entry.get("supported", False):
        print(f"error: compilation through the {adapter} adapter is not supported "
              f"in this release; select a supported adapter "
              f"({'opencode'} or {'claude-code'})",
              file=sys.stderr)
        return 2

    source_path = compiler.resolve_compile_source(repo, args.source)

    # Default output: openspec/plans/<source-stem>.toml
    if args.output is None:
        output_rel = f"openspec/plans/{source_path.stem}.toml"
        output_path = (repo / output_rel).resolve()
        if output_path.exists() and not args.force:
            raise base.PlanError(
                f"output exists: {output_path}  (use --force to overwrite)"
            )
        output_path.parent.mkdir(parents=True, exist_ok=True)
    else:
        output_path = compiler.resolve_compile_output(repo, args.output, args.force)
    model, controller_variant = compiler.check_controller_model(repo, adapter=adapter)

    client_name = entry["executable"]
    variant_note = f", variant: {controller_variant}" if controller_variant else ""
    base.log(f"compile: {source_path} -> {output_path}  "
        f"(adapter: {adapter}, client: {client_name}, model: {model}{variant_note})")

    source_content = source_path.read_text(encoding="utf-8")
    prompt = compiler.build_compile_prompt(source_content, source_path, repo, adapter=adapter)
    base.log(f"  prompt size: {len(prompt)} chars")

    base.log(f"  invoking {client_name} ...")
    stdout, stderr = compiler.run_compile_client(repo, adapter, model, prompt,
                                                 controller_variant,
                                                 timeout_minutes=timeout_minutes)
    if stderr.strip():
        base.log(f"  {client_name} stderr: {stderr.strip()[:500]}")

    toml_text = compiler.extract_toml(stdout, adapter=adapter)
    if not toml_text:
        raise base.PlanError("extracted TOML payload is empty")

    # Validate through existing load_plan() path
    try:
        parsed = tomllib.loads(toml_text)
    except Exception as exc:
        raise base.PlanError(f"generated TOML is not valid TOML: {exc}")

    if not isinstance(parsed, dict):
        raise base.PlanError("generated manifest must be a TOML table")

    plan_table = parsed.get("plan", {})
    if not isinstance(plan_table, dict):
        raise base.PlanError("generated manifest [plan] must be a TOML table")

    changes = parsed.get("changes", [])
    if not isinstance(changes, list):
        raise base.PlanError("generated manifest [[changes]] must be an array of TOML tables")
    for index, change in enumerate(changes, 1):
        if not isinstance(change, dict):
            raise base.PlanError(
                f"generated manifest [[changes]] entry {index} must be a TOML table"
            )

    # Require the generated adapter field to match the selected adapter.
    generated_adapter = plan_table.get("adapter", "")
    if generated_adapter != adapter:
        raise base.PlanError(
            f"generated manifest adapter is '{generated_adapter}' but "
            f"compilation selected '{adapter}'; "
            f"the {client_name} output must use adapter = \"{adapter}\""
        )

    tmp_path = output_path.with_suffix(output_path.suffix + ".compile-tmp")
    try:
        tmp_path.write_text(toml_text, encoding="utf-8")
        try:
            cfg = planref.load_plan(tmp_path, repo=repo)
        except base.PlanError:
            raise
        except Exception as exc:
            raise base.PlanError(f"generated manifest failed validation: {exc}") from exc
    except base.PlanError:
        tmp_path.unlink(missing_ok=True)
        raise
    except OSError as exc:
        tmp_path.unlink(missing_ok=True)
        raise base.PlanError(f"could not stage generated manifest: {exc}") from exc

    os.replace(tmp_path, output_path)
    base.log(f"  validated: {len(cfg['order'])} changes, {cfg['changes'].get(cfg['order'][0], {}).get('phase', 'no-phase') or 'no phase'}")

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
        base.log(f"  active plan set to: {rel}")
    except ValueError:
        base.log(f"  warning: compiled plan {output_path} is outside the repo; cannot auto-activate")

    return 0


def cmd_archive_plan(args: argparse.Namespace) -> int:
    """opsx-plan archive-plan <manifest> — archive a plan manifest pair."""
    repo = Path(args.repo).resolve()
    plan_arg = args.plan

    # Resolve to absolute path relative to repo
    plan_path = (repo / plan_arg).resolve()
    if not plan_path.is_file():
        print(f"error: manifest not found: {plan_arg}", file=sys.stderr)
        return 2

    # Refuse targets already under archived/
    try:
        plan_rel = str(plan_path.relative_to(repo))
    except ValueError:
        print(f"error: manifest must be inside the repository: {plan_path}", file=sys.stderr)
        return 2

    if "openspec/plans/archived/" in plan_rel:
        print(f"error: manifest is already under openspec/plans/archived/: {plan_rel}", file=sys.stderr)
        return 2

    if not plan_rel.startswith("openspec/plans/"):
        print(f"error: manifest must be under openspec/plans/: {plan_rel}", file=sys.stderr)
        return 2

    # Determine the sibling .md path
    md_path = plan_path.with_suffix(".md")
    has_md = md_path.is_file()

    archived_dir = repo / "openspec" / "plans" / "archived"
    archived_dir.mkdir(parents=True, exist_ok=True)

    moved: list[str] = []

    # Move the .toml
    toml_dst = archived_dir / plan_path.name
    moved.append(str(plan_path.relative_to(repo)))
    _git_mv_or_rename(repo, plan_path, toml_dst)

    # Move the sibling .md when present
    if has_md:
        md_dst = archived_dir / md_path.name
        moved.append(str(md_path.relative_to(repo)))
        _git_mv_or_rename(repo, md_path, md_dst)

    # Clear the active-plan pointer when it referenced the archived plan
    active = planref.read_active_plan(repo)
    if active == plan_rel:
        pointer = planref.active_plan_pointer_path(repo)
        if pointer.is_file():
            pointer.unlink()

    print(f"Archived:")
    for path in moved:
        print(f"  {path} -> openspec/plans/archived/{Path(path).name}")
    if active == plan_rel:
        print(f"  Cleared active-plan pointer (was: {plan_rel})")
    print(f"  Move still needs committing; archive-plan does not create a commit.")
    return 0


def _git_mv_or_rename(repo: Path, src: Path, dst: Path) -> None:
    """Move *src* to *dst* using ``git mv`` for tracked files, plain rename otherwise."""
    try:
        rel_src = str(src.relative_to(repo))
    except ValueError:
        rel_src = str(src)
    res = subprocess.run(
        ["git", "ls-files", "--error-unmatch", rel_src],
        cwd=repo, capture_output=True, text=True,
    )
    if res.returncode == 0:
        subprocess.run(
            ["git", "mv", rel_src, str(dst.relative_to(repo))],
            cwd=repo, check=True, capture_output=True,
        )
    else:
        os.rename(src, dst)


# ---------------------------------------------------------------------------
# Report command: implementation lives in lib/orchestrator/report.py
# ---------------------------------------------------------------------------


# ---------------------------------------------------------------------------
# Dashboard command: implementation lives in lib/orchestrator/dashboard.py
# ---------------------------------------------------------------------------


# ---------------------------------------------------------------------------
# models subcommand group
# ---------------------------------------------------------------------------

def _resolve_models_adapter(args: argparse.Namespace, repo: Path) -> str:
    """Resolve the target adapter for ``models show``/``models env``.

    Uses ``--adapter`` when given. Otherwise resolves the active plan the
    same way other operator commands do, so the two subcommands can run
    with no plan active as long as ``--adapter`` is supplied.
    """
    adapter = getattr(args, "adapter", None)
    if adapter:
        return adapter
    plan_src = planref.resolve_plan(repo, None)
    cfg = planref.load_plan(planref._resolve_plan_path(repo, plan_src), repo=repo)
    return cfg["adapter"]


def cmd_models_show(args: argparse.Namespace) -> int:
    """opsx-plan models show [--adapter <name>] — print resolved models."""
    repo = Path(args.repo).resolve()
    try:
        adapter = _resolve_models_adapter(args, repo)
    except base.PlanError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2
    try:
        resolved = resolve_models(adapter, repo=repo)
    except ModelConfigError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2

    print(f"adapter: {adapter}")
    for role in ALL_ROLES:
        entry = resolved[role]
        value = entry.model if entry.model else "(unresolved)"
        print(f"  {role:<12} {value}  [{entry.source}]")
        if entry.variant:
            print(f"  {'':<12} variant: {entry.variant}  [{entry.variant_source}]")

    warnings = validate_models(adapter, resolved)
    if warnings:
        print("\nidentifier-syntax warnings:")
        for warning in warnings:
            print(f"  - {warning}")

    return 0


def cmd_models_env(args: argparse.Namespace) -> int:
    """opsx-plan models env [--adapter <name>] — print shell export statements."""
    repo = Path(args.repo).resolve()
    try:
        adapter = _resolve_models_adapter(args, repo)
    except base.PlanError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2
    try:
        resolved = resolve_models(adapter, repo=repo)
    except ModelConfigError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2

    unresolved = [role for role in ROLES if not resolved[role].model]
    if unresolved:
        print(
            f"error: cannot emit environment for adapter '{adapter}': "
            f"unresolved role(s): {', '.join(unresolved)}",
            file=sys.stderr,
        )
        return 1

    for role in ROLES:
        print(f"export {ROLE_ENV[role]}={shlex.quote(resolved[role].model)}")
    # Emit the escalation export only when resolved.
    esc_entry = resolved.get("implementer_escalation")
    if esc_entry and esc_entry.model:
        print(f"export {ROLE_ENV['implementer_escalation']}={shlex.quote(esc_entry.model)}")
    # Emit reasoning-variant exports only when resolved; the installer keeps
    # the agent file's built-in default when no variant is configured.
    for role in ALL_ROLES:
        entry = resolved.get(role)
        if entry and entry.variant:
            print(f"export {ROLE_VARIANT_ENV[role]}={shlex.quote(entry.variant)}")
    return 0


def cmd_models_init(args: argparse.Namespace) -> int:
    """opsx-plan models init [--force] — seed the user-global config file."""
    path = USER_CONFIG_PATH
    if path.exists() and not getattr(args, "force", False):
        print(
            f"error: {path} already exists; use --force to overwrite",
            file=sys.stderr,
        )
        return 1

    lines = [
        "# Generated by `opsx-plan models init`.",
        "# See models.example.toml in the opsx-controller repo for the full",
        "# per-adapter precedence explanation.",
        "",
        "[defaults]",
    ]
    seeded = 0
    for role in ALL_ROLES:
        value = os.environ.get(ROLE_ENV[role], "").strip()
        if value:
            lines.append(f"{role} = {json.dumps(value)}")
            seeded += 1
    if seeded == 0:
        lines.append("# no OPSX_*_MODEL variables were set in the environment;")
        lines.append("# edit this file directly, e.g.:")
        lines.append('# controller = "github-copilot/gpt-5.4"')

    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    if seeded:
        print(f"Created {path} ({seeded} role(s) seeded from the environment)")
    else:
        print(f"Created {path} (no environment values found; edit it directly)")
    return 0


def cmd_doctor(args: argparse.Namespace) -> int:
    """opsx-plan doctor [plan] — run preflight checks."""
    repo = Path(args.repo).resolve()

    # Try to resolve plan; continue without plan if resolution fails and
    # no explicit plan argument was given.
    plan_src: str | None = None
    if args.plan is not None:
        # Explicit plan argument: fail hard when plan can't be resolved/loaded.
        plan_src = args.plan
        plan_abs = planref._resolve_plan_path(repo, plan_src)
        if not plan_abs.is_file():
            print(f"error: plan not found: {plan_src}", file=sys.stderr)
            return 2
    else:
        try:
            plan_src = planref.resolve_plan(repo, None)
        except base.PlanError:
            # Surface stale active-plan pointer errors; do not swallow them.
            pointer = planref.read_active_plan(repo)
            if pointer:
                plan_path = repo / pointer
                if not plan_path.is_file():
                    print(
                        f"warning: active plan pointer references missing file: {pointer}",
                        file=sys.stderr,
                    )
                    print(
                        "  Set a new active plan with: opsx-plan use <plan.toml>",
                        file=sys.stderr,
                    )
                else:
                    print(
                        f"error: active plan could not be loaded: {pointer}",
                        file=sys.stderr,
                    )
                    return 2
            # No active plan; run plan-independent checks only.

    # Determine adapter from resolved plan or explicit --adapter flag.
    adapter = getattr(args, "adapter", None) or "opencode"
    cfg: dict | None = None
    if plan_src:
        try:
            cfg = planref.load_plan(planref._resolve_plan_path(repo, plan_src), repo=repo)
            # A resolved plan's adapter is authoritative; --adapter is ignored.
            adapter = cfg["adapter"]
        except base.PlanError:
            print(f"error: cannot load plan: {plan_src}", file=sys.stderr)
            return 2

    print(f"opsx-plan doctor (repo: {repo})")
    if plan_src:
        print(f"  plan: {plan_src}")
    else:
        print("  plan: (none; running plan-independent checks only)")
    print()

    failures = run_doctor_checks(repo, plan_src, adapter, cfg)

    print()
    if failures:
        print(f"{failures} check(s) failed")
        return 1
    print("All checks passed")
    return 0


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
        budget_usd = 0.0
        i = 1
        while i < len(sys.argv):
            if sys.argv[i] == "--repo" and i + 1 < len(sys.argv):
                repo_arg = sys.argv[i + 1]
                i += 2
            elif sys.argv[i] == "--budget-usd" and i + 1 < len(sys.argv):
                budget_usd = float(sys.argv[i + 1])
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

        args = argparse.Namespace(repo=repo_arg, change=change_id, budget_usd=budget_usd)
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
    p_run.add_argument("--budget-usd", type=float, default=0)
    p_run.add_argument("--create-only", action="store_true",
                       help="create+verify ready changes without driving them")
    p_run.add_argument("--no-branch", action="store_true",
                       help="skip delivery branch creation on first run "
                            "(rejected if branch already recorded)")
    p_run.add_argument("--no-pr", action="store_true",
                       help="skip PR-delivery preflight and completion-time "
                            "PR creation for this invocation only")
    p_run.add_argument("--skip-warning", action="store_true",
                       help="treat review warnings and suggestions as non-blocking; "
                            "only critical findings prevent archive")
    p_run.add_argument("--skip-suggestion", action="store_true",
                       help="treat review suggestions as non-blocking; "
                            "critical and warning findings still prevent archive")
    p_run.add_argument("--skip-openspec", action="store_true",
                       help="skip the fail-closed OpenSpec-initialization gate "
                            "(the repo has no openspec/config.yaml; dispatch may "
                            "fail when workers cannot find their prompt files)")
    p_run.set_defaults(fn=cmd_run)

    p_status = sub.add_parser("status", help="reconcile and show plan status")
    p_status.add_argument("plan", nargs="?", default=None, help="path to plan TOML")
    p_status.set_defaults(fn=cmd_status)

    p_approve = sub.add_parser("approve", help="approve pause_before changes")
    p_approve.add_argument("plan", nargs="?", default=None, help="path to plan TOML")
    p_approve.add_argument("change", nargs="*")
    p_approve.add_argument(
        "--all", dest="approve_all", action="store_true",
        help="approve all changes currently awaiting approval",
    )
    p_approve.set_defaults(fn=cmd_approve)

    p_accept = sub.add_parser(
        "accept", help="accept orchestrator-created changes for driving"
    )
    p_accept.add_argument("plan", nargs="?", default=None, help="path to plan TOML")
    p_accept.add_argument("change", nargs="*")
    p_accept.add_argument(
        "--all", dest="accept_all", action="store_true",
        help="accept all changes currently awaiting acceptance",
    )
    p_accept.set_defaults(fn=cmd_accept)

    p_reset = sub.add_parser("reset", help="reset a failed change to pending")
    p_reset.add_argument("plan", nargs="?", default=None, help="path to plan TOML")
    p_reset.add_argument("change", nargs="*")
    p_reset.add_argument(
        "--failed", action="store_true",
        help="reset all failed changes to pending",
    )
    p_reset.set_defaults(fn=cmd_reset)

    p_compile = sub.add_parser(
        "compile", help="compile a markdown plan to TOML"
    )
    p_compile.add_argument(
        "source", help="path to source markdown plan (.md)"
    )
    p_compile.add_argument(
        "-o", "--output", default=None, help="output TOML path (default: openspec/plans/<source-stem>.toml)"
    )
    p_compile.add_argument(
        "--force", action="store_true", help="overwrite existing output"
    )
    p_compile.add_argument(
        "--adapter", default="opencode",
        choices=list(compiler.COMPILE_CLIENTS),
        help="adapter to compile against (default: opencode)",
    )
    p_compile.add_argument(
        "--timeout-minutes", type=float, default=10.0,
        help="compile client timeout in minutes (default: 10.0)",
    )
    p_compile.set_defaults(fn=cmd_compile)

    p_archive_plan = sub.add_parser(
        "archive-plan", help="archive a plan manifest pair to openspec/plans/archived/"
    )
    p_archive_plan.add_argument("plan", help="path to plan manifest TOML")
    p_archive_plan.set_defaults(fn=cmd_archive_plan)

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
    p_report.add_argument(
        "--for-change", default=None,
        help="target the derived single-change manifest instead of a plan "
             "path (mutually exclusive with positional plan)",
    )
    p_report.set_defaults(fn=report.cmd_report)

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
    p_dashboard.add_argument(
        "--for-change", default=None,
        help="target the derived single-change manifest instead of a plan "
             "path (mutually exclusive with positional plan)",
    )
    p_dashboard.set_defaults(fn=dashboard.cmd_dashboard)

    p_run_one = sub.add_parser(
        "run-one", help="run a single authored OpenSpec change directly"
    )
    p_run_one.add_argument("change", help="change id")
    p_run_one.add_argument("--budget-usd", type=float, default=0)
    p_run_one.add_argument("--skip-warning", action="store_true",
                           help="treat review warnings and suggestions as non-blocking; "
                                "only critical findings prevent archive")
    p_run_one.add_argument("--skip-suggestion", action="store_true",
                           help="treat review suggestions as non-blocking; "
                                "critical and warning findings still prevent archive")
    p_run_one.set_defaults(fn=cmd_run_one)

    p_doctor = sub.add_parser(
        "doctor", help="run preflight checks before a run"
    )
    p_doctor.add_argument("plan", nargs="?", default=None, help="path to plan TOML")
    p_doctor.add_argument(
        "--adapter", default=None,
        choices=list(compiler.COMPILE_CLIENTS) if compiler.COMPILE_CLIENTS else None,
        help="adapter to preflight (default: plan's adapter, or opencode when no plan)",
    )
    p_doctor.set_defaults(fn=cmd_doctor)

    p_models = sub.add_parser(
        "models", help="inspect and seed per-adapter model configuration"
    )
    models_sub = p_models.add_subparsers(dest="models_cmd", required=True)

    p_models_show = models_sub.add_parser(
        "show", help="print resolved models, their source, and any syntax warnings"
    )
    p_models_show.add_argument(
        "--adapter", default=None,
        help="adapter to resolve against (default: active plan's adapter)",
    )
    p_models_show.set_defaults(fn=cmd_models_show)

    p_models_env = models_sub.add_parser(
        "env", help="print shell export statements for the four resolved variables"
    )
    p_models_env.add_argument(
        "--adapter", default=None,
        help="adapter to resolve against (default: active plan's adapter)",
    )
    p_models_env.set_defaults(fn=cmd_models_env)

    p_models_init = models_sub.add_parser(
        "init", help="seed ~/.config/opsx-controller/models.toml from the environment"
    )
    p_models_init.add_argument(
        "--force", action="store_true", help="overwrite an existing file"
    )
    p_models_init.set_defaults(fn=cmd_models_init)

    p_logs = sub.add_parser(
        "logs", help="inspect the latest or filtered stage log for a resolved plan",
        description=(
            "Resolve the active or explicit plan, surface the most relevant "
            "stage log by default, support deterministic filtering by change "
            "and stage, list available logs, and follow an in-progress run."
        ),
    )
    p_logs.add_argument("plan", nargs="?", default=None, help="path to plan TOML")
    p_logs.add_argument(
        "--change", default=None,
        help="filter logs to this change id",
    )
    p_logs.add_argument(
        "--stage", default=None,
        help="filter logs to this stage (e.g. implement, review, archive)",
    )
    p_logs.add_argument(
        "--list", action="store_true",
        help="enumerate available matching logs instead of tailing one",
    )
    p_logs.add_argument(
        "--follow", action="store_true",
        help="follow the selected log like tail -f for an in-progress run",
    )
    p_logs.set_defaults(fn=cmd_logs)

    args = ap.parse_args()
    try:
        return args.fn(args)
    except base.PlanError as exc:
        print(f"plan error: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    sys.exit(main())
