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
import types
import time
import uuid
from pathlib import Path

# Resolve bundled runtime modules before considering the host repository.
#
# The installed entrypoints live in a sibling of the installed runtime package:
#   global:   ~/.local/bin/opsx-plan  ->  ~/.local/lib/opsx-controller/lib
#   project:  <project>/.opsx-controller/bin/opsx-plan
#           ->  <project>/.opsx-controller/lib
# The checkout tree (_SCRIPT_ROOT) is only a development fallback: an
# installed executable must resolve its runtime by its own location, never by
# importing from a repository checkout.
_SCRIPT_ROOT = Path(__file__).resolve().parents[1]
_RUNTIME_ROOTS = (
    _SCRIPT_ROOT,
    _SCRIPT_ROOT / "lib" / "opsx-controller",
    _SCRIPT_ROOT / ".opsx-controller",
)


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
    from lib.models.resolver import ModelConfigError
    from lib.models.resolver import resolve as resolve_models
    from lib.models.types import (
        ROLE_ENV,
        ROLE_VARIANT_ENV,
        ROLES,
        ALL_ROLES,
        OPTIONAL_ROLES,
    )
except ModuleNotFoundError as exc:  # pragma: no cover
    sys.exit(f"opsx-plan requires the lib.models runtime package: {exc}")

try:
    from lib.orchestrator import (
        base, compiler, cmd_archive_plan, cmd_doctor, cmd_gates, cmd_logs,
        cmd_models, cmd_run_one, cmd_status, cmd_supervise, cmd_use, dashboard,
        delivery, doctor, groundtruth, logs, planref, report,
        telemetry,
    )
    from lib.orchestrator import cost as cost_mod
    from lib.orchestrator import state as state_mod
    from lib.orchestrator import supervision as supervision
    from lib.supervisor import broker as broker_mod
    from lib.supervisor import budgets as budget_mod
    from lib.supervisor import ledger as ledger_mod
    from lib.supervisor import lifecycle as lifecycle_mod
    from lib.supervisor import lock as lock_mod
    from lib.supervisor import acceptance as acceptance_mod
    from lib.supervisor import agent_contracts as agent_contracts_mod
    from lib.supervisor import recovery as recovery_mod
except ModuleNotFoundError as exc:  # pragma: no cover
    sys.exit(f"opsx-plan requires the lib.orchestrator runtime package: {exc}")
base._RUNTIME_ROOTS = _RUNTIME_ROOTS

# Load journal integration only after a supervised registration is found.
# Ordinary runs must not import or enter the supervisor dispatch boundary.
journal_dispatch = None


def _load_journal_dispatch():
    global journal_dispatch
    if journal_dispatch is None:
        from lib.orchestrator import journal_dispatch as integration

        journal_dispatch = integration
    return journal_dispatch

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
            "pause_before_human_only": False,
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
        # The loader default is context-dependent (absent on a gated change
        # resolves human-only), so the key is written only when it changes the
        # loaded result: a gated change that delegates.  `true` is never
        # written — redundant on a gated change, invalid on an ungated one.
        if c.get("pause_before", False) and not c.get(
            "pause_before_human_only", True
        ):
            lines.append("pause_before_human_only = false")
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
    cfg.setdefault("_manifest_path", str(manifest_path))


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
        "id", "phase", "depends_on", "pause_before", "pause_before_human_only",
        "enabled",
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

    # Export every resolved optional role; explicitly unset every unresolved
    # optional role so a previously-set value from an earlier
    # apply_model_env call does not leak into a dispatch. This generalizes
    # the former hard-coded implementer_escalation handling to all optional
    # roles (escalation plus the supervised roles).
    for role in OPTIONAL_ROLES:
        entry = models.get(role)
        if entry and entry.model:
            os.environ[ROLE_ENV[role]] = entry.model
        else:
            os.environ.pop(ROLE_ENV[role], None)

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
    if stage == "acceptance":
        acceptance_state = r.get("acceptance", {}) or {}
        lines.append(
            "ACCEPTANCE_ARTIFACT_REVISION: "
            f"{acceptance_state.get('artifact_revision', '')}"
        )
        lines.append(
            "ACCEPTANCE_MANIFEST_SNAPSHOT_HASH: "
            f"{acceptance_state.get('manifest_snapshot_hash', '')}"
        )
        lines.append(
            "ACCEPTANCE_DEPENDS_ON: "
            + ", ".join(acceptance_state.get("depends_on", []) or [])
        )
        lines.append(
            "ACCEPTANCE_ARTIFACTS: "
            + ", ".join(acceptance_state.get("reviewed_artifacts", []) or [])
        )
        lines.append(
            "ACCEPTANCE_ACCEPT_RULE: an accept verdict's artifacts_reviewed "
            "must name exactly the ACCEPTANCE_ARTIFACTS set; a partial, "
            "arbitrary, or manifest/dependency-omitting accept is rejected"
        )
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
        # Workers occasionally wrap their required final JSON in Markdown
        # inline-code backticks — either a single unclosed prefix backtick or a
        # matching pair around the object. Strip both sides so the JSON line is
        # recognized either way.
        if "`" in candidate:
            candidate = candidate.strip("`").strip()
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
        if "`" in candidate:
            candidate = candidate.strip("`").strip()
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
    # Only the exact canonical active path counts: a sibling directory whose
    # name merely ends in ``-<cid>`` (which groundtruth.change_dir would
    # suffix-match) is an unrelated change and must not fail verification.
    # ``lexists`` also catches symlinks — including dangling ones — so link
    # debris at the canonical path fails verification and routes to
    # reactivation, which fails closed on non-directories.
    if os.path.lexists(repo / "openspec" / "changes" / cid):
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
    integration = journal_dispatch
    supervised_active = (
        integration is not None and integration.active_dispatch() is not None
    )

    def _resolve_active(outcome: str) -> None:
        if not supervised_active:
            return
        context = integration.active_dispatch()
        if context is None:
            return
        integration.resolve_dispatch(
            {"ledger": context["ledger"], "job_id": context["job_id"]},
            action_id=context["action_id"],
            outcome=outcome,
            record=None,
        )

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
            old_mask = None
            if supervised_active and hasattr(signal, "pthread_sigmask"):
                old_mask = signal.pthread_sigmask(signal.SIG_BLOCK, {signal.SIGINT})
            try:
                proc = subprocess.Popen(
                    cmd,
                    cwd=repo,
                    stdout=lf,
                    stderr=subprocess.STDOUT,
                    start_new_session=True,
                    env=os.environ.copy(),
                )
                _current_proc = proc
                if supervised_active:
                    try:
                        integration.note_spawned_process(proc.pid)
                    except Exception:
                        terminate_group(proc)
                        integration.mark_active_uncertain(
                            "worker identity could not be persisted"
                        )
                        _current_proc = None
                        return "spawn_error", log_path
            finally:
                if old_mask is not None:
                    signal.pthread_sigmask(signal.SIG_SETMASK, old_mask)
            try:
                proc.wait(timeout=timeout_s)
                return "exited", log_path
            except subprocess.TimeoutExpired:
                terminate_group(proc)
                if supervised_active:
                    integration.mark_active_uncertain("worker timed out after spawn")
                return "timeout", log_path
            except BaseException:
                terminate_group(proc)
                if supervised_active:
                    integration.mark_active_uncertain("worker wait failed after spawn")
                raise
            finally:
                _current_proc = None
    except FileNotFoundError:
        _resolve_active("spawn_error")
        return "spawn_error", log_path
    except OSError:
        _resolve_active("spawn_error")
        return "spawn_error", log_path
    except BaseException:
        if supervised_active and integration.active_dispatch() is not None:
            integration.mark_active_uncertain("worker spawn outcome is unknown")
        raise


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


def apply_review_result(
    repo: Path, cfg: dict, state: dict, cid: str, payload: dict, *,
    supervised: bool = False,
    gate: dict | None = None,
) -> str:
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
        # A registered supervised job runs the acceptance stage between a
        # review pass and archive. Legacy unregistered runs are unchanged and
        # still advance straight to archive.
        r["phase"] = "acceptance" if supervised else "archive"
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
                # For a registered supervised job the recurrence is routed to
                # bounded incident recovery instead of turning terminal on the
                # spot: the change is marked failed only when recovery escalates
                # or its bounded attempts are exhausted. An unregistered run
                # has no gate and keeps the existing terminal halt.
                recovery = begin_supervised_recovery(
                    gate,
                    cid,
                    "review",
                    {
                        "failure_class": "recurring_findings",
                        "message": reason,
                        "locus": locus,
                    },
                    discriminator=f"locus:{locus}",
                    run_id=state.get("run_id", ""),
                )
                if isinstance(recovery, dict) and recovery.get("status") == "recovering":
                    r["recovery"] = {
                        "incident_id": recovery.get("incident_id"),
                        "failure_class": recovery.get("failure_class"),
                        "signature": recovery.get("signature"),
                        "origin_stage": "review",
                        "summary": reason,
                    }
                    r["phase"] = "recovery"
                    state_mod.set_status(
                        state, cid, base.PENDING,
                        f"{reason}; bounded incident recovery is pending",
                    )
                    base.log(
                        f"  {cid}: {reason}; routed to bounded incident recovery "
                        f"(incident {recovery.get('incident_id')})"
                    )
                    return "continue"
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


# ---------------------------------------------------------------------------
# Acceptance stage (supervised-only artifact review between review and archive)
# ---------------------------------------------------------------------------

#: The change's authored artifacts, hashed into the acceptance artifact
#: revision. ``design.md`` is optional and hashed as absent when missing.
ACCEPTANCE_AUTHORED_ARTIFACTS = ("proposal.md", "design.md", "tasks.md")


def _read_optional_text(path: Path) -> str | None:
    try:
        return path.read_text(encoding="utf-8")
    except OSError:
        return None


def collect_acceptance_review_set(
    repo: Path, cfg: dict, state: dict, cid: str, *, manifest_snapshot_hash: str = ""
) -> dict:
    """Collect the change's real artifacts into a canonical review set.

    Reads the authored artifacts, the change's spec deltas (with the canonical
    specs they reference), and the tracked change files the run has recorded.
    Failures to read a file are hashed as ``absent`` rather than raising: a
    missing artifact is itself a revision-changing fact acceptance must see.
    """
    r = state_mod.rec(state, cid)
    cdir = groundtruth.change_dir(repo, cid)
    authored: dict[str, str | None] = {}
    for name in ACCEPTANCE_AUTHORED_ARTIFACTS:
        authored[f"openspec/changes/{cid}/{name}"] = _read_optional_text(cdir / name)

    spec_deltas: dict[str, str | None] = {}
    canonical_specs: dict[str, str | None] = {}
    specs_dir = cdir / "specs"
    if specs_dir.is_dir():
        for path in sorted(specs_dir.rglob("*.md")):
            if not path.is_file():
                continue
            rel = str(path.relative_to(repo))
            spec_deltas[rel] = _read_optional_text(path)
            canonical_rel = acceptance_mod.canonical_spec_rel_path(rel)
            if canonical_rel and canonical_rel not in canonical_specs:
                canonical_specs[canonical_rel] = _read_optional_text(repo / canonical_rel)

    tracked: dict[str, str | None] = {}
    seen: set[str] = set()
    for rel in list(r.get("tracked_change_files", []) or []) + list(
        state_mod.change_context_paths(repo, cid)
    ):
        normalized = acceptance_mod.normalize_rel_path(rel)
        if not normalized or normalized in seen:
            continue
        seen.add(normalized)
        tracked[normalized] = _read_optional_text(repo / normalized)

    return acceptance_mod.build_review_set(
        manifest_snapshot_hash=manifest_snapshot_hash,
        depends_on=cfg["changes"][cid].get("depends_on", []),
        authored_artifacts=authored,
        spec_deltas=spec_deltas,
        canonical_specs=canonical_specs,
        tracked_change_files=tracked,
    )


def compute_acceptance_revision(
    repo: Path, cfg: dict, state: dict, cid: str, *, gate: dict | None = None
) -> tuple[str, dict, list[str]]:
    """Return ``(revision, review_set, authoritative_identities)`` for the change now.

    The third element is the engine-derived authoritative artifact-identity
    list for the review set — the manifest/dependency ground truth plus every
    file artifact — never a worker-supplied claim.
    """
    snapshot_hash = ""
    if gate is not None:
        snapshot_hash = str(gate.get("manifest_snapshot_hash") or "")
    review_set = collect_acceptance_review_set(
        repo, cfg, state, cid, manifest_snapshot_hash=snapshot_hash
    )
    revision = acceptance_mod.artifact_revision(review_set)
    return revision, review_set, acceptance_mod.authoritative_artifact_identities(review_set)


def prepare_acceptance_attempt(
    repo: Path, cfg: dict, state: dict, cid: str, r: dict, *, gate: dict | None = None
) -> dict:
    """Run the created-change check and capture the revision it validated.

    The configured created-change check runs first via the existing
    ``groundtruth.verify_change_created`` path; the artifact revision is then
    captured immediately, so the verdict the reviewer returns binds to content
    that passed validation at the moment of review. A failing check blocks the
    stage with the recorded reason and no ``accept`` is recorded. An unresolved
    escalation is a blocking state that must be returned to the primary before
    a fresh acceptance can run.
    """
    proj = r["acceptance"]
    if proj.get("blocking") and proj.get("outcome") == acceptance_mod.ESCALATE:
        return {
            "blocked": (
                "acceptance escalation is unresolved; the primary session must "
                "return a judgment before the change can advance to archive"
            ),
            "last_result": "acceptance_escalation_unresolved",
            "retryable": True,
        }
    try:
        ok, why = groundtruth.verify_change_created(repo, cfg, cid)
    except Exception as exc:  # noqa: BLE001 - a check crash is a failed check
        ok, why = False, f"created-change check raised: {exc}"
    if not ok:
        proj["created_check"] = why or "failed"
        proj["updated_at"] = base.utcnow()
        return {
            "blocked": f"created-change check blocked acceptance: {why}",
            "last_result": "acceptance_created_check_failed",
        }
    proj["created_check"] = "passed"
    revision, review_set, reviewed = compute_acceptance_revision(
        repo, cfg, state, cid, gate=gate
    )
    proj["artifact_revision"] = revision
    proj["reviewed_artifacts"] = reviewed
    # The manifest/dependency ground truth is captured with the revision so the
    # reviewer is shown, and an accept is bound to, the protected plan identity
    # rather than only the file artifacts.
    proj["manifest_snapshot_hash"] = str(
        review_set.get("manifest_snapshot_hash", "") or ""
    )
    proj["depends_on"] = list(review_set.get("depends_on", []) or [])
    proj["updated_at"] = base.utcnow()
    return {"revision": revision, "reviewed_artifacts": reviewed}


def _dispatch_session_identity(ledger, action_id) -> str:
    """Best-available session identity for one dispatched action.

    The journal's bound dispatch session is authoritative when present; when
    session binding is unavailable (the orchestrator does not bind a session id
    today) the action identity is used, because two distinct dispatches are two
    distinct worker sessions. This is what lets the existing repair gate treat
    the fixer and verifier as independent sessions.
    """
    if ledger is None or action_id is None:
        return ""
    try:
        dispatch = ledger.latest_dispatch(int(action_id))
    except Exception:  # noqa: BLE001 - identity absence is not a crash
        dispatch = None
    if dispatch is not None and dispatch["session_id"]:
        return str(dispatch["session_id"]).strip()
    return f"action:{int(action_id)}"


def apply_acceptance_result(
    repo: Path,
    cfg: dict,
    state: dict,
    cid: str,
    payload: dict,
    *,
    gate: dict | None = None,
    action_id: int | None = None,
) -> str:
    """Apply one acceptance reviewer verdict to the run's control flow.

    Records the verdict against the revision the reviewer judged, rejects a
    stale ``accept`` as unsatisfying the stage, routes ``fix`` to the pinned
    fixer (bounded by the change round budget), and returns an ``escalate`` to
    the primary as blocking state that stops the change before archive.
    """
    r = state_mod.rec(state, cid)
    proj = r["acceptance"]
    # Compute the authoritative artifact identity set before trusting any
    # worker claim: an accept binds to the set the reviewer was shown at
    # prepare time (falling back to the freshly computed set), never to a
    # worker-supplied list alone.
    recorded_revision = str(proj.get("artifact_revision", "") or "")
    current_revision, _review_set, current_reviewed = compute_acceptance_revision(
        repo, cfg, state, cid, gate=gate
    )
    required_artifacts = list(proj.get("reviewed_artifacts", []) or []) or list(
        current_reviewed
    )
    try:
        verdict = acceptance_mod.normalize_verdict(
            payload, required_artifacts=required_artifacts
        )
    except acceptance_mod.AcceptanceContractError as exc:
        proj["outcome"] = ""
        proj["stale"] = False
        proj["blocking"] = False
        proj["updated_at"] = base.utcnow()
        r["last_result"] = "acceptance_invalid"
        state_mod.set_status(state, cid, base.FAILED, f"acceptance output invalid: {exc}")
        _try_notify(cfg, "change_failed", f"acceptance output invalid: {exc}", change_id=cid)
        return "stop"

    outcome = verdict["outcome"]
    # A verdict binds to the revision the reviewer judged; the freshly computed
    # revision decides staleness. An approval receipt can never stand in: this
    # reads only the acceptance verdict and the artifacts.
    stale = acceptance_mod.revision_is_stale(recorded_revision, current_revision)
    reviewed = verdict["artifacts_reviewed"] or list(
        proj.get("reviewed_artifacts", []) or current_reviewed
    )
    proj.update(
        {
            "outcome": outcome,
            "artifact_revision": recorded_revision or current_revision,
            "reviewed_artifacts": reviewed,
            "reason": verdict["reason"],
            "fix_prompt": verdict["fix_prompt"],
            "stale": stale,
            "escalated": outcome == acceptance_mod.ESCALATE,
            "blocking": outcome == acceptance_mod.ESCALATE,
            "updated_at": base.utcnow(),
        }
    )
    if outcome == acceptance_mod.ACCEPT:
        proj["blocking"] = False
        proj["escalated"] = False

    if gate is not None:
        try:
            gate["ledger"].record_acceptance_review(
                int(gate["job_id"]),
                change_id=cid,
                outcome=outcome,
                artifact_revision=recorded_revision or current_revision,
                reviewed_artifacts=reviewed,
                reason=verdict["reason"],
                fix_prompt=verdict["fix_prompt"],
                dispatch_action_id=action_id,
                session_id=_dispatch_session_identity(
                    gate.get("ledger"), action_id
                )
                or None,
                created_check_evidence=proj.get("created_check", ""),
            )
        except Exception as exc:  # noqa: BLE001 - a lost verdict must fail closed
            # The verdict is authoritative only once its ledger row exists. A
            # failed durable write must not satisfy the stage or drive a
            # transition: keep the change out of archive/fix/escalate, clear the
            # unrecorded outcome from the projection, and surface the named
            # persistence error instead of advancing.
            reason = (
                f"acceptance verdict persistence failed for {cid} "
                f"(outcome={outcome}): {type(exc).__name__}: {exc}"
            )
            base.log(f"error: {reason}")
            proj["outcome"] = ""
            proj["persistence_error"] = reason
            proj["stale"] = False
            proj["escalated"] = False
            proj["blocking"] = False
            proj["updated_at"] = base.utcnow()
            r["last_result"] = "acceptance_persistence_error"
            state_mod.set_status(state, cid, base.FAILED, reason)
            _try_notify(cfg, "change_failed", reason, change_id=cid)
            return "stop"

    append_history(
        state,
        cid,
        {
            "round": r["round"],
            "phase": "acceptance",
            "status": outcome,
            "summary": verdict["reason"] or f"acceptance {outcome}",
            "artifact_revision": recorded_revision or current_revision,
            "stale": stale,
            "reviewed_artifacts": reviewed,
        },
    )

    if outcome == acceptance_mod.ESCALATE:
        proj["escalation_round"] = r["round"]
        reason = verdict["reason"] or "hard judgment required"
        r["last_result"] = "acceptance_escalated"
        state_mod.set_status(
            state,
            cid,
            base.PENDING,
            f"acceptance escalation returned to the primary session: {reason}",
        )
        _try_notify(
            cfg,
            "acceptance_escalated",
            f"change {cid} acceptance escalated to the primary: {reason}",
            change_id=cid,
        )
        return "stop"

    if outcome == acceptance_mod.FIX:
        defect = verdict["fix_prompt"] or verdict["reason"] or "unspecified acceptance defect"
        r["latest_fix_prompt"] = verdict["fix_prompt"]
        if r["round"] >= r["max_rounds"]:
            reason = f"acceptance fix budget exhausted; unrepaired defect: {defect}"
            r["last_result"] = "acceptance_fix_exhausted"
            state_mod.set_status(state, cid, base.FAILED, reason)
            _try_notify(cfg, "change_failed", reason, change_id=cid)
            return "stop"
        r["round"] += 1
        r["phase"] = "fix"
        r["last_result"] = "acceptance_fix_requested"
        state_mod.set_status(
            state, cid, base.PENDING, f"acceptance requested a mechanical fix: {defect}"
        )
        return "continue"

    # accept
    r["latest_fix_prompt"] = ""
    if stale:
        reason = (
            "acceptance verdict is stale: the reviewed artifact revision no "
            "longer matches the artifacts under review"
        )
        proj["stale"] = True
        if r["round"] >= r["max_rounds"]:
            r["last_result"] = "acceptance_stale_budget_exhausted"
            state_mod.set_status(state, cid, base.FAILED, f"{reason}; round budget exhausted")
            _try_notify(cfg, "change_failed", reason, change_id=cid)
            return "stop"
        r["round"] += 1
        r["phase"] = "acceptance"
        r["last_result"] = "acceptance_stale"
        state_mod.set_status(state, cid, base.PENDING, f"{reason}; rerunning acceptance")
        return "continue"
    proj["stale"] = False
    r["last_result"] = "acceptance_passed"
    r["phase"] = "archive"
    state_mod.set_status(state, cid, base.PENDING, verdict["reason"] or "acceptance passed")
    return "continue"


def apply_fix_result(
    repo: Path,
    cfg: dict,
    state: dict,
    cid: str,
    payload: dict,
    *,
    action_id: int | None = None,
) -> str:
    """Apply one fixer report: a claim that is never self-certifying.

    The report is stored as a claim and the change advances to the independent
    ``verify`` stage. It marks nothing complete and satisfies nothing on its
    own; only the verifier's verdict on the real diff can make the repair
    consumable.
    """
    r = state_mod.rec(state, cid)
    proj = r["acceptance"]
    payload = payload if isinstance(payload, dict) else {}
    role = str(payload.get("role") or "fixer").strip()
    if role != "fixer":
        r["last_result"] = "fix_invalid"
        state_mod.set_status(
            state, cid, base.FAILED, f"fixer returned unexpected role={role}"
        )
        _try_notify(cfg, "change_failed", f"fixer returned unexpected role={role}", change_id=cid)
        return "stop"
    checks = payload.get("checks")
    report = {
        "repair": str(payload.get("repair") or ""),
        "files": [str(path) for path in payload.get("files", []) if isinstance(path, str)],
        "checks": checks if isinstance(checks, list) else [],
        # A fixer report is a claim; ``self_certified`` is always false here
        # regardless of what the worker sent.
        "self_certified": False,
    }
    proj["fix"] = report
    proj["verified"] = False
    proj["fix_action_id"] = action_id
    proj["updated_at"] = base.utcnow()
    append_history(
        state,
        cid,
        {
            "round": r["round"],
            "phase": "fix",
            "status": "reported",
            "summary": report["repair"] or "fixer reported a repair",
            "files": report["files"],
        },
    )
    r["phase"] = "verify"
    r["last_result"] = "fix_reported"
    state_mod.set_status(
        state, cid, base.PENDING, "fixer reported a repair; awaiting independent verification"
    )
    return "continue"


def apply_verify_result(
    repo: Path,
    cfg: dict,
    state: dict,
    cid: str,
    payload: dict,
    *,
    gate: dict | None = None,
    action_id: int | None = None,
) -> str:
    """Apply one verifier verdict and enforce the repair-consumability gate.

    A repair is consumed only when the independent verifier validated the real
    diff (``pass`` + ``repair_verified`` + ``diff_reviewed``) in a session
    distinct from the fixer's. The decision is recorded as durable journal
    evidence so the next gated dispatch re-reads and re-enforces it. A failed
    or unverifiable repair fails the change naming the unrepaired defect.
    """
    r = state_mod.rec(state, cid)
    proj = r["acceptance"]
    payload = payload if isinstance(payload, dict) else {}
    ledger = gate.get("ledger") if gate is not None else None
    fixer_report = proj.get("fix") or None
    fixer_action_id = proj.get("fix_action_id")
    fixer_session = _dispatch_session_identity(ledger, fixer_action_id)
    verifier_session = _dispatch_session_identity(ledger, action_id)

    if not fixer_report:
        reason = "no fixer report exists to verify; refusing a repair with no claim"
        proj["verified"] = False
        proj["blocking"] = False
        proj["updated_at"] = base.utcnow()
        r["last_result"] = "repair_unverified"
        state_mod.set_status(state, cid, base.FAILED, reason)
        _try_notify(cfg, "change_failed", reason, change_id=cid)
        return "stop"

    enriched_fixer = dict(fixer_report)
    enriched_fixer["session_id"] = fixer_session
    enriched_verdict = dict(payload)
    enriched_verdict["session_id"] = verifier_session
    decision = agent_contracts_mod.repair_consumable(
        fixer_report=enriched_fixer,
        verifier_verdict=enriched_verdict,
        fixer_session_id=fixer_session or None,
        verifier_session_id=verifier_session or None,
    )
    if ledger is not None and action_id is not None:
        # Attach the claim+verdict pair to the action that produced the repair.
        # The repair gate reads the fixer session identity from that action's
        # dispatch record, so recording the pair on the verify action would
        # collapse the two session identities and wrongly refuse the repair as
        # not independently verified.
        evidence_action_id = (
            fixer_action_id if fixer_action_id is not None else action_id
        )
        try:
            agent_contracts_mod.record_repair_evidence(
                ledger,
                int(evidence_action_id),
                fixer_report=enriched_fixer,
                verifier_verdict=enriched_verdict,
                decision=decision,
            )
        except Exception as exc:  # noqa: BLE001 - a repair needs durable evidence
            # A repair may only be consumed against durable independent-verifier
            # evidence. A failed write must not advance to a fresh acceptance or
            # leave the repair gate reading an unrecorded verdict: fail closed
            # naming the persistence error instead.
            reason = (
                f"repair evidence persistence failed for {cid}: "
                f"{type(exc).__name__}: {exc}"
            )
            base.log(f"error: {reason}")
            proj["verified"] = False
            proj["blocking"] = False
            proj["persistence_error"] = reason
            proj["updated_at"] = base.utcnow()
            r["last_result"] = "repair_evidence_persistence_error"
            state_mod.set_status(state, cid, base.FAILED, reason)
            _try_notify(cfg, "change_failed", reason, change_id=cid)
            return "stop"

    append_history(
        state,
        cid,
        {
            "round": r["round"],
            "phase": "verify",
            "status": "consumable" if decision["consumable"] else "blocked",
            "summary": decision["reason"] or "repair independently verified",
            "verifier_verdict": decision.get("verifier_verdict"),
        },
    )
    if decision["consumable"]:
        proj["verified"] = True
        proj["blocking"] = False
        proj["updated_at"] = base.utcnow()
        r["phase"] = "acceptance"
        r["last_result"] = "repair_verified"
        state_mod.set_status(
            state,
            cid,
            base.PENDING,
            "independent verifier validated the repair; running a fresh acceptance",
        )
        return "continue"

    defect = str(
        proj.get("fix_prompt")
        or (fixer_report or {}).get("repair")
        or "acceptance defect"
    )
    reason = f"unrepaired acceptance defect ({defect}): {decision['reason']}"
    proj["verified"] = False
    proj["updated_at"] = base.utcnow()
    r["last_result"] = "repair_unverified"
    state_mod.set_status(state, cid, base.FAILED, reason)
    _try_notify(cfg, "change_failed", reason, change_id=cid)
    return "stop"


def resolve_acceptance_escalation(state: dict, cid: str, *, note: str = "") -> None:
    """Primary action: resolve an unresolved acceptance escalation.

    Clears the blocking escalation state and returns the change to the
    acceptance stage so a fresh acceptance runs over the (possibly corrected)
    revision. This is the only sanctioned way past the escalation boundary —
    the engine never defaults an ``escalate`` to ``accept`` or ``fix``.
    """
    r = state_mod.rec(state, cid)
    proj = r["acceptance"]
    if not proj.get("escalated") and not proj.get("blocking"):
        return
    proj["blocking"] = False
    proj["escalated"] = False
    proj["reason"] = note or "escalation resolved by the primary session"
    proj["updated_at"] = base.utcnow()
    r["phase"] = "acceptance"
    r["last_result"] = "acceptance_escalation_resolved"
    state_mod.set_status(state, cid, base.PENDING, proj["reason"])


def reactivate_archived_change(repo: Path, cid: str, archive: dict) -> tuple[bool, str]:
    """Move an archived change back to its active location for a fresh round.

    Supervised fresh-review revalidation requeues a change whose post-archive
    evidence failed, but the implement/review/archive loop resolves a change
    only at its active ``openspec/changes/<id>`` location — after a valid
    archive that directory is necessarily absent. The archived artifacts are
    therefore moved back before the fresh round is queued, which also frees
    the dated archive name so the next archive worker run can produce fresh
    dated evidence. Audit evidence is preserved: any ``archive(<id>):`` commit
    stays reachable in git history, and the caller records a ``reactivated``
    history entry with the source archive path and the failure reason. The
    move is a no-op success when the change already sits at its active
    location — e.g. the archive evidence itself was bogus and nothing ever
    moved.

    The recorded archive path comes from an unverified worker payload or a
    mutable state file, so it is never trusted to select the move source.
    Reactivation fails closed unless the recorded path is repo-relative and
    resolves to exactly the canonical dated archive directory for this change
    — derived independently via ``groundtruth.find_archive_dir`` — with both
    ends confined beneath ``openspec/changes/archive``. Absolute, traversing,
    mismatched, or symlink-escaping paths are rejected before any filesystem
    mutation.

    The existence check and the move target use the exact canonical active
    path ``openspec/changes/<cid>`` — never ``groundtruth.change_dir``'s
    loose suffix matching — so an unrelated sibling directory whose name
    merely ends in ``-<cid>`` neither satisfies the no-op check nor receives
    the restored artifacts. Only a real, non-symlink directory at that path
    satisfies the no-op check: a symlink, dangling link, or plain file there
    is rejected before any filesystem mutation, so the fresh loop can never
    be requeued to follow a link into unrelated artifacts.
    """
    # Only a real, non-symlink directory at the exact canonical active path
    # counts as "already active": a sibling directory whose name merely ends
    # in ``-<cid>`` is an unrelated change, and a symlink (even one resolving
    # to a directory) at the canonical path would satisfy ``exists()`` and
    # skip the required move, letting the fresh loop follow the link into
    # unrelated artifacts. Anything else occupying the canonical path — a
    # symlink, a dangling link, or a plain file — is hostile debris, so fail
    # closed before any filesystem mutation rather than requeueing or
    # clobbering it.
    change_dir = repo / "openspec" / "changes" / cid
    if os.path.lexists(change_dir):
        if change_dir.is_dir() and not change_dir.is_symlink():
            return True, ""
        return False, (
            f"openspec/changes/{cid} exists but is not a real directory "
            "(symlink, dangling link, or file), refusing reactivation"
        )
    archive_path = archive.get("path", "")
    if not archive_path:
        return False, "no archive path recorded to reactivate from"
    if Path(archive_path).is_absolute():
        return False, (
            f"archive path is absolute, refusing reactivation: {archive_path}"
        )
    canonical = groundtruth.find_archive_dir(repo, cid)
    if canonical is None:
        return False, (
            f"no canonical dated archive directory found for {cid}, "
            "cannot reactivate"
        )
    archive_root = (repo / "openspec" / "changes" / "archive").resolve()
    canonical_resolved = canonical.resolve()
    if not canonical_resolved.is_relative_to(archive_root):
        return False, (
            f"canonical archive directory escapes openspec/changes/archive, "
            f"refusing reactivation: {canonical.name}"
        )
    src = (repo / archive_path).resolve()
    if src != canonical_resolved:
        return False, (
            f"archive path is not the canonical dated archive directory for "
            f"{cid}, refusing reactivation: {archive_path}"
        )
    if not src.is_dir():
        return False, f"archive path missing, cannot reactivate: {archive_path}"
    try:
        change_dir.parent.mkdir(parents=True, exist_ok=True)
        shutil.move(str(src), str(change_dir))
    except OSError as exc:
        return False, f"could not reactivate {archive_path}: {exc}"
    return True, ""


def _archive_revalidation_failed(
    repo: Path,
    cfg: dict,
    state: dict,
    cid: str,
    r: dict,
    reason: str,
    *,
    last_result: str | None,
    supervised: bool,
    gate: dict | None = None,
) -> str:
    """Handle failed post-archive completion evidence for an archived change.

    A supervised job never treats the prior archive as proof of done: while
    the change's existing round budget remains, the archived change is
    reactivated at its active OpenSpec location and the change reruns a fresh
    review round through the existing implement/review/archive loop; the
    failure turns terminal only when that budget is exhausted or the archived
    artifacts can no longer be reactivated. A legacy unregistered run keeps
    its previous terminal-failure behavior unchanged.
    """
    archive = r["archive"]
    archive["status"] = "failed"
    archive["reason"] = reason
    if supervised:
        # Route the partial archive / failed post-archive evidence into
        # bounded recovery for a registered job: the recovery driver
        # revalidates the change through the existing review loop and the
        # prior archive is never treated as done. Post-archive revalidation
        # failures — an unverified archive, a failed fast check, or a dirty
        # tracked tree — are all fresh-review revalidation. An unregistered
        # run has no gate and consults no recovery code.
        recovery = begin_supervised_recovery(
            gate,
            cid,
            "archive",
            {
                "failure_class": "partial_archive",
                "message": reason,
            },
            remedy=recovery_mod.REMEDY_FRESH_REVIEW,
        )
        if isinstance(recovery, dict) and recovery.get("status") == "recovering":
            if last_result is not None:
                r["last_result"] = last_result
            r["recovery"] = {
                "incident_id": recovery.get("incident_id"),
                "failure_class": recovery.get("failure_class"),
                "signature": recovery.get("signature"),
                "origin_stage": "archive",
                "summary": reason,
            }
            r["phase"] = "recovery"
            state_mod.set_status(
                state, cid, base.PENDING,
                f"{reason}; bounded incident recovery is pending",
            )
            base.log(
                f"  {cid}: {reason}; routed to bounded incident recovery "
                f"(incident {recovery.get('incident_id')})"
            )
            return "continue"
    elif last_result is not None:
        r["last_result"] = last_result
    state_mod.set_status(state, cid, base.FAILED, reason)
    _try_notify(cfg, "change_failed", reason, change_id=cid)
    return "stop"


def apply_archive_result(
    repo: Path,
    cfg: dict,
    state: dict,
    cid: str,
    payload: dict,
    *,
    supervised: bool = False,
    gate: dict | None = None,
) -> str:
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
        return _archive_revalidation_failed(
            repo, cfg, state, cid, r, f"archive unverified: {why}",
            last_result=None, supervised=supervised, gate=gate,
        )
    checks_ok, check_why = groundtruth.run_fast_checks(repo, cfg)
    if not checks_ok:
        return _archive_revalidation_failed(
            repo, cfg, state, cid, r, f"post-archive {check_why}",
            last_result="post_archive_check_failed", supervised=supervised, gate=gate,
        )
    clean_ok, clean_why = delivery.verify_post_archive_clean(repo, cfg)
    if not clean_ok:
        return _archive_revalidation_failed(
            repo, cfg, state, cid, r, f"post-archive {clean_why}",
            last_result="post_archive_dirty_tracked", supervised=supervised, gate=gate,
        )
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


# ---------------------------------------------------------------------------
# Supervised budget gate (durable reservations and reconciliation)
# ---------------------------------------------------------------------------


class SupervisionGateError(Exception):
    """A registered supervised job's gate could not be read safely.

    Raised instead of returning ``None`` when supervision state is present but
    inaccessible, so a registered job fails closed: the run blocks rather than
    treating unreadable supervision as "no supervision" and dispatching
    unaccounted through the legacy path.
    """


def open_supervised_gate(repo: Path, manifest_path: Path | str | None = None) -> dict | None:
    """Return the supervised budget gate for *repo*, or ``None``.

    A gate exists only for a worktree with a non-terminal registered supervised
    job; an ordinary, unregistered run returns ``None`` and keeps the legacy
    ``--budget-minutes`` / ``--budget-usd`` behavior with no durable budget
    layer. Detection routes through the same authority-validated,
    service-owned store lookup the gate and run commands use, so a worker
    cannot substitute the registration decision with a missing or repointed
    path; a genuinely absent supervision backend is ``None``.

    Opening fails closed: when a supervision backend is configured but the
    ledger or its policy cannot be read — including a configured store that
    does not exist — this raises :class:`SupervisionGateError` so the caller
    blocks before any dispatch rather than silently taking the unregistered
    legacy path.

    The gate carries the registration anchors — the policy operator revision
    and the protected manifest-snapshot hash captured when the gate opened —
    so each action revalidates the immutable plan and policy before dispatch.
    """
    try:
        registration = supervision.open_registration(repo)
    except broker_mod.BrokerError as exc:
        raise SupervisionGateError(str(exc)) from exc
    if registration is None:
        return None
    return {
        "ledger": registration.ledger,
        "job_id": registration.job_id,
        "policy": registration.policy,
        "policy_revision": int(registration.policy["revision"]),
        "manifest_snapshot_hash": registration.policy["manifest_snapshot_hash"],
        "manifest_path": str(manifest_path) if manifest_path is not None else None,
    }


def close_supervised_gate(gate: dict | None) -> None:
    if not gate:
        return
    try:
        gate["ledger"].close()
    except Exception:
        pass


def _assert_run_gates_resolvable(registration, cfg: dict) -> None:
    """Refuse a supervised dispatch whose gates are not broker-resolved.

    Gate authority for a registered job comes from broker receipts, not from
    ``state["approvals"]``. Resume revalidation runs first so a relied-upon
    receipt that no longer matches the current material revision re-arms its
    gate and raises ``StaleMaterialError`` into the run rather than
    dispatching. Legacy ``classify()`` is untouched for unregistered runs.
    """
    broker_mod.assert_resume_clear(registration.ledger, registration.job_id)
    for cid in cfg["order"]:
        change = cfg["changes"][cid]
        if not change.get("pause_before"):
            continue
        if not broker_mod.is_dispatchable(
            registration.ledger, registration.job_id, cid
        ):
            resolution = broker_mod.resolve_gate(
                registration.ledger, registration.job_id, cid
            )
            raise broker_mod.StaleMaterialError(
                f"change {cid} is not dispatchable: {resolution.reason}; approve "
                "it through the operator path before running"
            )


def execution_elapsed_minutes(ledger, job_id: int) -> float:
    return _load_journal_dispatch().execution_elapsed_minutes(ledger, job_id)


def supervised_gate_reserve(
    repo: Path, cfg: dict, gate: dict, cid: str, stage: str, round_num: int,
    r: dict, run_id: str,
) -> dict:
    """Reserve budget for one supervised dispatch (reservation mechanics).

    This is the budget/deadline/incident mechanics without the pre-dispatch
    gates; the engine's dispatch path calls :func:`supervised_gated_reserve`,
    which evaluates the lock/authority/freshness/model gates first. Kept as a
    public entry for the reservation boundary itself.
    """
    return _load_journal_dispatch().reserve_for_dispatch(
        repo, cfg, gate, cid, stage, round_num, r, run_id
    )


def supervised_gated_reserve(
    repo: Path, cfg: dict, gate: dict, cid: str, stage: str, round_num: int,
    r: dict, run_id: str, *, resolved_model: str | None,
) -> dict:
    """Run the full gated dispatch boundary, surfacing blocks as blocked states.

    Delegates to :func:`journal_dispatch.gated_dispatch` and converts its named
    gate error into the run loop's durable blocked-state vocabulary.
    """
    integration = _load_journal_dispatch()
    try:
        return integration.gated_dispatch(
            repo, cfg, gate, cid, stage, round_num, r, run_id,
            resolved_model=resolved_model,
        )
    except integration.DispatchGateError as exc:
        return {"blocked": str(exc), "last_result": exc.last_result}


def evaluate_supervised_completion(
    repo: Path, cfg: dict, ledger: Any, job_id: int
) -> tuple[bool, list[dict[str, str]], dict[str, list[str]]]:
    """Return ``(complete, failures, manual_tasks)`` for a supervised job.

    Completion is decided only from the existing ground truth — archive
    evidence per enabled change (:func:`verify_direct_archive_done`), the
    post-archive fast checks (:func:`groundtruth.run_fast_checks`), and
    post-archive cleanliness (:func:`delivery.verify_post_archive_clean`) —
    never from a worker or primary session's claim of done. Pending
    ``(manual)`` tasks are collected as the operator checklist and never make
    the job incomplete.
    """
    state = state_mod.load_state(repo, cfg["name"])
    manual: dict[str, list[str]] = {}
    failures: list[dict[str, str]] = []
    for cid in cfg["order"]:
        change = cfg["changes"].get(cid) or {}
        if not change.get("enabled", True):
            continue
        record = state_mod.rec(state, cid)
        ok, why = verify_direct_archive_done(repo, cid, record)
        if not ok:
            failures.append({"change_id": cid, "reason": why})
            continue
        pending = state_mod.pending_manual_tasks(repo, cid)
        if pending:
            manual[cid] = pending
    if failures:
        return False, failures, manual
    checks_ok, check_why = groundtruth.run_fast_checks(repo, cfg)
    if not checks_ok:
        return False, [{"change_id": "", "reason": f"post-archive {check_why}"}], manual
    clean_ok, clean_why = delivery.verify_post_archive_clean(repo, cfg)
    if not clean_ok:
        return False, [{"change_id": "", "reason": f"post-archive {clean_why}"}], manual
    return True, [], manual


def _finalize_supervised_completion(repo: Path, cfg: dict, registration: Any) -> None:
    """Verify and record supervised completion at the end of a run.

    Verification reads plan, archive, and fast-check evidence. A verified job
    transitions to ``completed`` and the pending ``(manual)`` tasks are attached
    to the completion record and printed as the operator checklist; an
    unverified job records a durable incident naming the outstanding change and
    stays non-terminal (the existing loop remains the progression authority).
    """
    ledger = registration.ledger
    job_id = int(registration.job_id)
    try:
        complete, failures, manual = evaluate_supervised_completion(
            repo, cfg, ledger, job_id
        )
    except broker_mod.BrokerError as exc:
        print(f"error: {type(exc).__name__}: {exc}", file=sys.stderr)
        return
    manual_flat = [
        f"{cid}: {task}" for cid, tasks in manual.items() for task in tasks
    ]
    if not complete:
        reasons = "; ".join(
            f"{failure['change_id'] or 'plan'}: {failure['reason']}"
            for failure in failures
        )
        try:
            ledger.record_incident(job_id, kind="completion_blocked", summary=reasons)
        except Exception:
            pass
        print(f"supervised completion not verified: {reasons}", file=sys.stderr)
        return
    try:
        job = lifecycle_mod.complete(ledger, job_id)
    except broker_mod.BrokerError as exc:
        print(f"error: {type(exc).__name__}: {exc}", file=sys.stderr)
        return
    summary = "job completed from archive evidence and post-archive checks"
    if manual_flat:
        summary += "; pending manual tasks: " + "; ".join(manual_flat)
    try:
        ledger.record_incident(job_id, kind="job_completed", summary=summary)
    except Exception:
        pass
    print(f"supervised job {job_id} is {job['state']}")
    if manual_flat:
        print("operator checklist (pending manual tasks):")
        for item in manual_flat:
            print(f"  - {item}")


def supervised_gate_record_incident(
    gate: dict, cid: str, stage: str, *, discriminator: str = "dispatch",
) -> None:
    """Record one failing dispatch attempt against its stable signature.

    The counter is incremented only when a dispatch actually fails, so a clean
    dispatch never consumes the bounded-attempts budget and the count survives
    ``opsx-plan reset`` in the external ledger.
    """
    try:
        signature = budget_mod.incident_signature(
            kind=stage, change_id=cid, stage=stage, discriminator=discriminator
        )
        gate["ledger"].record_incident_attempt(gate["job_id"], signature=signature)
    except Exception:
        pass


def supervised_recovery_context(gate: dict | None) -> tuple | None:
    """Return ``(ledger, job_id)`` for a registered gate, or ``None``.

    This is the single registered-job guard every recovery hook consults: an
    unregistered run has no gate, so it enters no recovery code and keeps its
    legacy behavior unchanged.
    """
    if not isinstance(gate, dict):
        return None
    ledger = gate.get("ledger")
    job_id = gate.get("job_id")
    if ledger is None or job_id is None:
        return None
    return ledger, int(job_id)


def supervised_recovery_policy(gate: dict, ledger, job_id: int) -> dict:
    """Return the job's current protected policy for recovery decisions."""
    policy = gate.get("policy")
    if isinstance(policy, dict) and policy:
        return policy
    try:
        return ledger.current_policy(int(job_id))
    except Exception:  # noqa: BLE001 - absent policy is durable-state absence
        return {}


def begin_supervised_recovery(
    gate: dict | None,
    cid: str,
    stage: str,
    failure,
    *,
    remedy: str | None = None,
    discriminator: str = "",
    run_id: str | None = None,
) -> dict | None:
    """Classify a supervised failure and enter bounded incident recovery.

    Every hook is guarded on the registered-job gate: an unregistered run
    (``gate is None``) returns ``None`` without consulting any recovery code.
    A durable-write failure is likewise reported as ``None`` so the caller
    keeps its existing terminal behavior rather than crashing the run.
    """
    context = supervised_recovery_context(gate)
    if context is None:
        return None
    ledger, job_id = context
    policy = supervised_recovery_policy(gate, ledger, job_id)
    try:
        result = recovery_mod.begin_recovery(
            ledger,
            job_id=job_id,
            change_id=str(cid),
            stage=str(stage),
            failure=failure,
            policy=policy,
            remedy=remedy,
            discriminator=discriminator,
            run_id=run_id,
        )
    except recovery_mod.RemedyPolicyViolation:
        return {"status": "escalated", "failure_class": "policy_violation"}
    except Exception:  # noqa: BLE001 - recovery is best-effort over the journal
        return None
    if result.get("status") == "recovering":
        try:
            recovery_mod.bounded_recovery_attempt(
                ledger,
                job_id=job_id,
                signature=str(result.get("signature", "")),
                policy=policy,
            )
        except recovery_mod.BoundedRecoveryExceeded as exc:
            escalated = recovery_mod.escalate_incident(
                ledger,
                int(result["incident_id"]),
                reason=str(exc),
                operator_action="review the recurring incident and intervene",
            )
            result = dict(result)
            result["status"] = "escalated"
            result["blocker"] = escalated["blocker"]
        except Exception:  # noqa: BLE001 - the bound is best-effort durable
            pass
    return result


class RecoveryDispatchError(Exception):
    """A recovery stage dispatch failed before producing a usable payload."""

    def __init__(self, message: str, *, evidence: dict | None = None) -> None:
        super().__init__(message)
        self.evidence = evidence if isinstance(evidence, dict) else {"message": message}


#: Recovery paths whose remedy is executed by the cheap fixer plus an
#: independent verifier, with the resuming effect gated on the verdict and
#: the job's standing grant.
_REPAIR_RECOVERY_PATHS = frozenset(
    {
        "corrective_redispatch",
        "recurring_findings_repair",
        "worktree_preserving_repair",
        "delta_identity_repair",
    }
)


def _recovery_fail_change(
    state: dict, cid: str, cfg: dict, r: dict, reason: str, last_result: str
) -> str:
    r["last_result"] = last_result
    state_mod.set_status(state, cid, base.FAILED, reason)
    _try_notify(cfg, "change_failed", reason, change_id=cid)
    return "failed"


def _recovery_stage_dispatch(
    repo: Path,
    cfg: dict,
    state: dict,
    cid: str,
    r: dict,
    gate: dict,
    stage: str,
    *,
    input_block: str | None = None,
) -> tuple[dict, int | None]:
    """Dispatch one stage through the journaled supervised boundary.

    Every recovery dispatch crosses the same reserve/invoke/reconcile journal
    boundary as an ordinary stage dispatch. Returns ``(payload, action_id)``
    for a parsed result; raises :class:`RecoveryDispatchError` — carrying
    failure-classification evidence — on a gate block, a non-clean outcome, or
    unparseable worker output.
    """
    run_id = state.get("run_id", "")
    resolved_model = _resolved_dispatch_model(repo, cfg, stage, r)
    entry = supervised_gated_reserve(
        repo, cfg, gate, cid, stage, r["round"], r, run_id,
        resolved_model=resolved_model,
    )
    if entry.get("blocked"):
        raise RecoveryDispatchError(str(entry["blocked"]))
    if entry.get("recovered"):
        raise RecoveryDispatchError(
            f"{stage} dispatch recovered a prior completion during recovery; "
            "recovery cannot reapply it"
        )
    if input_block is None:
        input_block = build_worker_input(repo, cfg, state, cid, stage=stage)
    outcome, log_path = invoke_direct_stage(
        repo, cfg, cid, stage, r["round"], input_block
    )
    integration = _load_journal_dispatch()
    integration.resolve_dispatch(
        gate,
        action_id=entry.get("action_id"),
        reservation_id=entry.get("reservation_id"),
        outcome=outcome,
        record=None,
    )
    payload, parse_why, _envelope = parse_stage_json(log_path)
    if outcome not in ("exited", "completed"):
        raise RecoveryDispatchError(
            f"{stage} dispatch ended with outcome={outcome}",
            evidence=dispatch_failure_evidence(
                outcome, {"message": f"{stage} ended with outcome={outcome}"}
            ),
        )
    if payload is None:
        raise RecoveryDispatchError(
            f"{stage} output invalid: {parse_why}",
            evidence=dispatch_failure_evidence(
                "invalid_output", {"message": parse_why}
            ),
        )
    return payload, entry.get("action_id")


def _apply_recovered_stage_payload(
    repo: Path,
    cfg: dict,
    state: dict,
    cid: str,
    r: dict,
    gate: dict,
    stage: str,
    payload: dict,
    action_id: int | None,
) -> str:
    """Apply the payload of a stage redispatched by bounded transient retry."""
    if stage == "implement":
        return apply_implement_result(repo, cfg, state, cid, payload)
    if stage == "review":
        return apply_review_result(
            repo, cfg, state, cid, payload, supervised=True, gate=gate
        )
    if stage == "acceptance":
        return apply_acceptance_result(
            repo, cfg, state, cid, payload, gate=gate, action_id=action_id
        )
    if stage == "fix":
        return apply_fix_result(repo, cfg, state, cid, payload, action_id=action_id)
    if stage == "verify":
        return apply_verify_result(
            repo, cfg, state, cid, payload, gate=gate, action_id=action_id
        )
    return apply_archive_result(
        repo, cfg, state, cid, payload, supervised=True, gate=gate
    )


def _route_resolved_recovery(r: dict, path: str, origin_stage: str) -> None:
    """Route a resolved recovery to the loop its bounded path requires.

    A recurring-findings repair earns a fresh review over the repaired work;
    every other resolved recovery returns to the stage that failed, and a
    fresh review over archive material re-enters the normal implement loop.
    """
    if path == recovery_mod.PATH_RECURRING_FINDINGS_REPAIR:
        r["phase"] = "review"
    elif path == recovery_mod.PATH_FRESH_REVIEW:
        r["phase"] = "implement"
    else:
        r["phase"] = origin_stage


def _drive_recovery_transient_retry(
    repo: Path,
    cfg: dict,
    state: dict,
    cid: str,
    r: dict,
    gate: dict,
    ledger,
    job_id: int,
    rec_ctx: dict,
    origin_stage: str,
) -> str:
    """Redispatch the failed stage under the bounded transient-retry path."""
    incident_id = int(rec_ctx["incident_id"])
    signature = str(rec_ctx.get("signature") or "")

    def _attempt() -> tuple[dict, int | None]:
        return _recovery_stage_dispatch(
            repo, cfg, state, cid, r, gate, origin_stage
        )

    try:
        payload, action_id = recovery_mod.bounded_transient_retry(
            ledger, job_id=job_id, signature=signature, operation=_attempt
        )
    except Exception as exc:  # noqa: BLE001 - any failure escalates the incident
        escalated = recovery_mod.escalate_incident(
            ledger,
            incident_id,
            reason=f"bounded transient retry did not clear the failure: {exc}",
            operator_action="inspect the provider failure and intervene",
        )
        reason = (
            f"transient recovery for incident {incident_id} escalated: "
            f"{escalated['reason']}"
        )
        return _recovery_fail_change(state, cid, cfg, r, reason, "recovery_escalated")
    recovery_mod.resolve_incident(
        ledger, incident_id, summary="bounded transient retry cleared the failure"
    )
    r["recovery"] = {}
    state_mod.set_status(
        state, cid, base.PENDING,
        f"transient {origin_stage} failure cleared by bounded retry",
    )
    return _apply_recovered_stage_payload(
        repo, cfg, state, cid, r, gate, origin_stage, payload, action_id
    )


def _drive_recovery_repair(
    repo: Path,
    cfg: dict,
    state: dict,
    cid: str,
    r: dict,
    gate: dict,
    ledger,
    job_id: int,
    policy: dict,
    rec_ctx: dict,
    path: str,
    origin_stage: str,
) -> str:
    """Run the fixer/independent-verifier repair and gate the resume effect."""
    incident_id = int(rec_ctx["incident_id"])
    failure_class = str(rec_ctx.get("failure_class") or "")
    run_id = state.get("run_id", "")

    def _dispatch(role: str, request: Mapping[str, Any]) -> dict:
        stage = "fix" if role == "fixer" else "verify"
        if not cfg.get(f"{stage}_invoke"):
            raise RecoveryDispatchError(
                f"{stage} invoke is not configured for adapter "
                f"'{cfg.get('adapter', '')}'; the recovery repair cannot run "
                "and is not skipped"
            )
        input_block = build_worker_input(repo, cfg, state, cid, stage=stage)
        input_block += (
            f"\nRECOVERY_INCIDENT_ID: {incident_id}"
            f"\nRECOVERY_FAILURE_CLASS: {failure_class}"
            f"\nRECOVERY_ROLE: {role}"
            f"\nRECOVERY_SUMMARY: {single_line(str(rec_ctx.get('summary') or ''))}"
        )
        payload, action_id = _recovery_stage_dispatch(
            repo, cfg, state, cid, r, gate, stage, input_block=input_block
        )
        result = dict(payload)
        result["action_id"] = action_id
        result["session_id"] = _dispatch_session_identity(ledger, action_id)
        return result

    try:
        repair = recovery_mod.dispatch_repair(
            ledger,
            job_id=job_id,
            change_id=cid,
            stage=origin_stage,
            incident_id=incident_id,
            dispatch=_dispatch,
            run_id=run_id,
            context={
                "failure_class": failure_class,
                "summary": str(rec_ctx.get("summary") or ""),
            },
        )
        recovery_mod.consume_recovery_effect(
            ledger,
            job_id=job_id,
            incident_id=incident_id,
            change_id=cid,
            effect="resume",
            policy=policy,
            verdict=repair["verifier_verdict"],
            fixer_report=repair["fixer_report"],
            fixer_session_id=repair.get("fixer_session_id"),
            verifier_session_id=repair.get("verifier_session_id"),
            # Record the claim+verdict pair on the fixer action, exactly as
            # the acceptance repair loop does: recording it on the verifier
            # action would collapse the two session identities and later
            # repair-gate reads would refuse the repair as not independent.
            action_id=repair["fixer_report"].get("action_id"),
            run_id=run_id,
        )
    except Exception as exc:  # noqa: BLE001 - any repair failure escalates
        escalated = recovery_mod.escalate_incident(
            ledger,
            incident_id,
            reason=f"recovery repair could not be consumed: {exc}",
            operator_action="review the failed repair and intervene",
        )
        reason = (
            f"recovery repair for incident {incident_id} escalated: "
            f"{escalated['reason']}"
        )
        return _recovery_fail_change(state, cid, cfg, r, reason, "recovery_escalated")
    recovery_mod.resolve_incident(
        ledger,
        incident_id,
        summary="fixer repair independently verified; the change resumed",
    )
    r["recovery"] = {}
    _route_resolved_recovery(r, path, origin_stage)
    append_history(
        state,
        cid,
        {
            "round": r["round"],
            "phase": "recovery",
            "status": "resolved",
            "summary": (
                f"incident {incident_id} repaired and independently verified; "
                f"resumed at {r['phase']}"
            ),
        },
    )
    state_mod.set_status(
        state, cid, base.PENDING,
        f"recovery repair verified; resumed at {r['phase']}",
    )
    base.log(
        f"  {cid}: recovery incident {incident_id} resolved; "
        f"resumed at {r['phase']}"
    )
    return "continue"


def _drive_recovery_reconciliation(
    repo: Path,
    cfg: dict,
    state: dict,
    cid: str,
    r: dict,
    gate: dict,
    ledger,
    job_id: int,
    rec_ctx: dict,
    origin_stage: str,
) -> str:
    """Reconcile the recorded interrupted action from evidence before resume.

    The process-interruption path re-attempts reconciliation of the specific
    action recorded in the recovery context through the journal evidence
    boundary. The change resumes only after that exact action is decisively
    reconciled as completed — journal or repository evidence confirms its
    completion. An action that is terminally failed, missing, mismatched, or
    otherwise unreconciled escalates the incident and fails the change
    terminally; the reconciliation-only pass never redispatches on an
    assumption, and any other pending action keeps blocking independently.
    """
    incident_id = int(rec_ctx["incident_id"])
    recorded_action_id = rec_ctx.get("action_id")

    def _escalate(message: str) -> str:
        decision = recovery_mod.reconcile_process_interruption(
            evidence={"message": message},
            action_id=(
                int(recorded_action_id) if recorded_action_id is not None else None
            ),
        )
        escalated = recovery_mod.escalate_incident(
            ledger,
            incident_id,
            reason=(
                message
                or decision["reason"]
                or "the interrupted action cannot be reconciled from evidence"
            ),
            operator_action="reconcile the interrupted action and intervene",
        )
        reason = (
            f"process-interruption recovery for incident {incident_id} "
            f"escalated: {escalated['reason']}"
        )
        return _recovery_fail_change(state, cid, cfg, r, reason, "recovery_escalated")

    if recorded_action_id is None:
        return _escalate(
            "the process-interruption recovery context does not record the "
            "interrupted action; the incident cannot be reconciled"
        )
    run_id = state.get("run_id", "") or telemetry.get_or_create_run_id(
        repo, cfg, state
    )
    resolved_model = _resolved_dispatch_model(repo, cfg, origin_stage, r)
    recovered = _recover_supervised_dispatch(
        repo,
        cfg,
        gate,
        cid,
        origin_stage,
        r["round"],
        r,
        run_id,
        resolved_model,
        reobserve=lambda: _reobserve_direct_stage_completion(
            repo, cid, origin_stage, r
        ),
        allow_replay=False,
        target_action_id=int(recorded_action_id),
    )
    if recovered is not None and recovered.get("recovered"):
        recovery_mod.resolve_incident(
            ledger,
            incident_id,
            summary=(
                f"interrupted action {int(recorded_action_id)} reconciled "
                "from decisive evidence"
            ),
        )
        r["recovery"] = {}
        if r["phase"] == "recovery":
            r["phase"] = origin_stage
        append_history(
            state,
            cid,
            {
                "round": r["round"],
                "phase": "recovery",
                "status": "resolved",
                "summary": (
                    f"incident {incident_id} reconciled from decisive "
                    f"evidence; resumed at {r['phase']}"
                ),
            },
        )
        state_mod.set_status(
            state, cid, base.PENDING,
            f"process interruption reconciled; resumed at {r['phase']}",
        )
        base.log(
            f"  {cid}: recovery incident {incident_id} resolved; "
            f"resumed at {r['phase']}"
        )
        return "continue"
    return _escalate(str((recovered or {}).get("blocked") or ""))


def drive_supervised_recovery(
    repo: Path, cfg: dict, state: dict, cid: str, r: dict, gate: dict | None
) -> str:
    """Execute the bounded recovery flow for a registered supervised job.

    This is the production driver for the ``recovery`` phase: it consumes the
    recovery context recorded when the failure was routed, executes the
    incident's bounded path — bounded transient retry, or the primary-chosen
    fixer/independent-verifier repair with standing-grant gating on the
    resuming effect — and routes a resolved recovery to the required
    normal/fresh-review loop. The change is marked failed only when the
    recovery escalates or its durable per-signature bound is exhausted. An
    unregistered run has no gate, is driven back onto the ordinary path, and
    consults no recovery code.
    """
    context = supervised_recovery_context(gate)
    if context is None:
        r["phase"] = "implement"
        return "continue"
    ledger, job_id = context
    rec_ctx = r.get("recovery") or {}
    incident_id = rec_ctx.get("incident_id")
    if incident_id is None:
        r["phase"] = "implement"
        return "continue"
    policy = supervised_recovery_policy(gate, ledger, job_id)
    try:
        row = ledger.get_incident(int(incident_id))
    except Exception:  # noqa: BLE001 - a missing incident cannot be driven
        r["phase"] = "implement"
        return "continue"
    incident_state = str(row["state"])
    failure_class = str(rec_ctx.get("failure_class") or row["kind"] or "")
    origin_stage = str(rec_ctx.get("origin_stage") or "implement")
    if origin_stage not in {"implement", "review", "acceptance", "fix", "verify", "archive"}:
        origin_stage = "implement"
    path = recovery_mod.CLASS_PATHS.get(failure_class, recovery_mod.PATH_ESCALATE)
    if incident_state == "escalated":
        reason = str(row["summary"] or f"incident {incident_id} escalated")
        return _recovery_fail_change(state, cid, cfg, r, reason, "recovery_escalated")
    if incident_state == "resolved":
        r["recovery"] = {}
        _route_resolved_recovery(r, path, origin_stage)
        return "continue"
    if path == recovery_mod.PATH_BOUNDED_TRANSIENT_RETRY:
        return _drive_recovery_transient_retry(
            repo, cfg, state, cid, r, gate, ledger, job_id, rec_ctx, origin_stage
        )
    if path in _REPAIR_RECOVERY_PATHS:
        return _drive_recovery_repair(
            repo, cfg, state, cid, r, gate, ledger, job_id, policy,
            rec_ctx, path, origin_stage,
        )
    if path == recovery_mod.PATH_UNCERTAIN_ACTION_RECONCILIATION:
        return _drive_recovery_reconciliation(
            repo, cfg, state, cid, r, gate, ledger, job_id, rec_ctx,
            origin_stage,
        )
    if path == recovery_mod.PATH_FRESH_REVIEW:
        # The remedy is the loop's own fresh review: no repair is produced, so
        # no consumption gate fires. The archived change is reactivated and
        # re-enters the implement/review/archive loop, bounded by the change's
        # existing round budget.
        archive = r["archive"]
        summary = str(rec_ctx.get("summary") or "post-archive evidence failed")
        if r["round"] < r["max_rounds"]:
            # Fresh-review revalidation, bounded by the change's round budget.
            # The loop can only resolve the change at its active location, so
            # the archived artifacts must be reactivated before requeueing;
            # without them the next round could not even find the change.
            restored, restore_why = reactivate_archived_change(repo, cid, archive)
            if not restored:
                reason = f"{summary}; cannot rerun a fresh review: {restore_why}"
                archive["reason"] = reason
                recovery_mod.escalate_incident(
                    ledger,
                    int(incident_id),
                    reason=reason,
                    operator_action="restore the archived change and intervene",
                )
                return _recovery_fail_change(
                    state, cid, cfg, r, reason, "recovery_escalated"
                )
            recovery_mod.resolve_incident(
                ledger,
                int(incident_id),
                summary="routed to a fresh review through the existing loop",
            )
            r["recovery"] = {}
            append_history(
                state,
                cid,
                {
                    "round": r["round"],
                    "phase": "archive",
                    "status": "reactivated",
                    "summary": (
                        f"post-archive evidence failed ({summary}); reactivated "
                        f"the archived change for a fresh review round"
                    ),
                    "archive_path": archive.get("path", ""),
                },
            )
            r["round"] += 1
            r["phase"] = "implement"
            state_mod.set_status(
                state, cid, base.PENDING,
                f"{summary}; fresh review round {r['round']}",
            )
            base.log(
                f"  {cid}: {summary}; reactivated the archived change and "
                f"rerun a fresh review round ({r['round']}/{r['max_rounds']})"
            )
            return "continue"
        reason = (
            f"{summary}; the change's round budget is exhausted; "
            "no fresh review remains"
        )
        recovery_mod.escalate_incident(
            ledger,
            int(incident_id),
            reason=reason,
            operator_action="review the change and intervene",
        )
        return _recovery_fail_change(state, cid, cfg, r, reason, "recovery_escalated")
    escalated = recovery_mod.escalate_incident(
        ledger,
        int(incident_id),
        reason=f"{failure_class} has no automated recovery path",
        operator_action="triage the failure and intervene",
    )
    reason = (
        f"recovery for incident {incident_id} escalated: {escalated['reason']}"
    )
    return _recovery_fail_change(state, cid, cfg, r, reason, "recovery_escalated")


def dispatch_failure_evidence(outcome: str, record: dict | None) -> dict:
    """Build classification evidence for a failed supervised dispatch.

    The mapping is evidence, never an authority: an explicit class is supplied
    only where the outcome itself names one (a timeout, an exhausted invalid
    result); otherwise the recorded reason is classified, and an unknown reason
    escalates rather than being guessed into a repair.
    """
    reason = ""
    if isinstance(record, dict):
        reason = str(record.get("reason") or record.get("message") or "")
    if outcome == "timeout":
        return {"failure_class": "transient_provider", "message": reason or "dispatch timed out"}
    if outcome == "invalid_output":
        return {"failure_class": "invalid_result", "message": reason or "invalid worker output"}
    return {"message": reason, "outcome": str(outcome)}


def create_outcome_state(outcome: str) -> str:
    """Map a ``run_stage`` outcome to the reconcile outcome vocabulary.

    ``exited`` is the only clean create outcome; every other value is a
    non-completed dispatch that retains/incidents consistently with the direct
    stage loop.
    """
    return "completed" if outcome == "exited" else outcome


def _effective_create_invoke(cfg: dict, cid: str, invoke_tpl: str) -> str:
    return (
        invoke_tpl.replace("{change}", cid)
        .replace("{plan_doc}", cfg["plan_doc"])
        .replace("{controller_model}", os.environ.get("OPSX_CONTROLLER_MODEL", ""))
    )


def _canonical_invocation_model(repo: Path, cfg: dict, invoke: str) -> str | None:
    effective = telemetry._best_effort_expand_invoke(invoke)
    parsed = telemetry._extract_invocation_model(effective, cfg["adapter"], repo)
    provider = parsed.get("provider")
    model_id = parsed.get("model_id")
    if isinstance(model_id, str) and model_id.startswith("{env:") and model_id.endswith("}"):
        model_id = os.environ.get(model_id[5:-1], "").strip() or None
        provider = None
        if isinstance(model_id, str) and "/" in model_id:
            provider, model_id = model_id.split("/", 1)
    if not model_id:
        return None
    return f"{provider}/{model_id}" if provider else str(model_id)


def _resolved_dispatch_model(
    repo: Path, cfg: dict, stage: str, r: dict, *, invoke: str | None = None
) -> str | None:
    command = invoke if invoke is not None else cfg.get(f"{stage}_invoke", "")
    resolved = _canonical_invocation_model(repo, cfg, command)
    if resolved is not None:
        return resolved
    if stage == "create":
        return None
    role = telemetry.resolve_stage_role(
        stage, escalation_active=bool(r.get("escalation", {}).get("active"))
    )
    env_name = ROLE_ENV.get(role or "", "")
    if not env_name:
        return None
    return os.environ.get(env_name, "").strip() or None


def _reobserve_direct_stage_completion(
    repo: Path, cid: str, stage: str, r: dict
) -> bool | None:
    """Return repository evidence for an interrupted same-stage action."""
    if stage == "archive":
        if not record_archive_evidence(repo, r, cid):
            return False
        return verify_direct_archive_done(repo, cid, r)[0]
    return None


def _recover_supervised_dispatch(
    repo: Path,
    cfg: dict,
    gate: dict,
    cid: str,
    stage: str,
    round_num: int,
    r: dict,
    run_id: str,
    resolved_model: str | None,
    *,
    reobserve,
    allow_replay: bool = True,
    target_action_id: int | None = None,
) -> dict | None:
    integration = _load_journal_dispatch()
    pending = integration.reconcile_pending(gate)
    if target_action_id is not None:
        # A process-interruption recovery reconciles exactly the recorded
        # interrupted action, never the job-wide pending inventory: another
        # pending action is neither conflated with it nor resolved by it and
        # keeps blocking its own dispatch independently.
        item = next(
            (
                entry
                for entry in pending
                if int(entry["action_id"]) == int(target_action_id)
            ),
            None,
        )
        if item is None:
            # The recorded action is no longer pending: inspect its journaled
            # state directly. Only a decisive completion of that exact action
            # reconciles the interruption; a failed, missing, or mismatched
            # record keeps the incident blocking.
            try:
                row = gate["ledger"].get_action(int(target_action_id))
            except Exception:  # noqa: BLE001 - a missing record is unreconciled
                return {
                    "blocked": (
                        f"recorded interrupted action {int(target_action_id)} "
                        "is missing from the journal"
                    ),
                    "last_result": "uncertain_action_pending",
                    "action_id": int(target_action_id),
                }
            try:
                recorded_detail = json.loads(row["detail"] or "{}")
            except (TypeError, ValueError):
                recorded_detail = {}
            if not isinstance(recorded_detail, dict):
                recorded_detail = {}
            item = {
                "action_id": int(target_action_id),
                "kind": row["kind"],
                "run_id": row["run_id"],
                "detail": row["detail"],
                "change_id": recorded_detail.get("change_id"),
                "stage": recorded_detail.get("stage"),
            }
        pending = [item]
    if not pending:
        return None
    if len(pending) != 1:
        ids = ", ".join(str(entry["action_id"]) for entry in pending)
        own = [
            entry
            for entry in pending
            if entry.get("change_id") == cid and entry.get("stage") == stage
        ]
        return {
            "blocked": f"supervised actions {ids} require individual reconciliation",
            "last_result": "uncertain_action_pending",
            # Attribute this dispatch attempt's own interrupted action when it
            # is uniquely identifiable, so the recovery phase reconciles that
            # recorded action rather than the job-wide inventory.
            "action_id": int(own[0]["action_id"]) if len(own) == 1 else None,
        }
    item = pending[0]
    if item.get("change_id") != cid:
        return {
            "blocked": (
                f"supervised action {item['action_id']} belongs to "
                f"{item.get('change_id') or 'an unknown change'}/"
                f"{item.get('stage') or item['kind']}; reconcile it before {cid}/{stage}"
            ),
            "last_result": "uncertain_action_pending",
            "action_id": item["action_id"],
        }
    pending_stage = item.get("stage")
    try:
        pending_detail = json.loads(item.get("detail") or "{}")
    except (TypeError, ValueError):
        pending_detail = {}
    if not isinstance(pending_detail, dict):
        pending_detail = {}
    stage_order = {
        "create": 0,
        "implement": 1,
        "review": 2,
        "acceptance": 3,
        "fix": 4,
        "verify": 5,
        "archive": 6,
    }
    if pending_stage not in stage_order or stage not in stage_order:
        return {
            "blocked": (
                f"supervised action {item['action_id']} has incomplete stage "
                "context; reconcile it before dispatch"
            ),
            "last_result": "uncertain_action_pending",
            "action_id": item["action_id"],
        }
    if stage_order[pending_stage] > stage_order[stage]:
        return {
            "blocked": (
                f"supervised action {item['action_id']} belongs to later stage "
                f"{pending_stage}; reconcile it before {cid}/{stage}"
            ),
            "last_result": "uncertain_action_pending",
            "action_id": item["action_id"],
        }

    def _reobserve_pending_action() -> bool | None:
        # Reaching a later stage is durable state evidence that the prior stage
        # completed before its journal transition was interrupted.
        if stage_order[pending_stage] < stage_order[stage]:
            return True
        if pending_stage == "implement":
            baseline = pending_detail.get("pending_automatable_tasks")
            if not isinstance(baseline, list) or not baseline:
                return None
            remaining = set(state_mod.remaining_automatable_tasks(repo, cid))
            complete = all(str(task) not in remaining for task in baseline)
            if complete or allow_replay:
                return complete
            # A reconciliation-only pass never redispatches: work observed
            # incomplete without decisive completion evidence stays
            # unreconciled instead of triggering a fenced replay.
            return None
        observed = reobserve()
        if observed is False and not allow_replay:
            return None
        return observed

    try:
        result = integration.replay_uncertain(
            repo,
            cfg,
            gate,
            item["action_id"],
            cid=cid,
            round_num=round_num,
            r=r,
            run_id=run_id,
            resolved_model=resolved_model,
            reobserve=_reobserve_pending_action,
        )
    except integration.DispatchGateError as exc:
        return {"blocked": str(exc), "last_result": exc.last_result}
    if result.get("replayed"):
        return result
    reason = result.get("reason")
    recovered_state = result.get("state")
    if reason == "observed_complete" or (
        reason in {"reconciled", "terminal"}
        and recovered_state == "completed"
        and _reobserve_pending_action() is True
    ):
        if pending_stage == stage == "implement":
            r["task_counts"] = state_mod.change_task_counts(repo, cid)
            r["phase"] = "review"
            r["status"] = base.PENDING
            r["reason"] = (
                "implementation completion recovered from repository evidence"
            )
            r["updated_at"] = base.utcnow()
        elif pending_stage == stage == "archive":
            r["last_result"] = "archive_passed"
            r["phase"] = "done"
            r["manual_tasks_pending"] = state_mod.pending_manual_tasks(repo, cid)
            r["status"] = base.DONE
            r["reason"] = "verified from repository archive evidence"
            r["updated_at"] = base.utcnow()
        return {
            "recovered": True,
            "action_id": item["action_id"],
            "state": "completed",
        }
    if reason in {"reconciled", "terminal"}:
        return {
            "blocked": (
                f"supervised action {item['action_id']} recovered as "
                f"{recovered_state or 'terminal'}, but {cid}/{stage} completion "
                "is not reflected in repository state"
            ),
            "last_result": "recovered_action_state_mismatch",
            "action_id": item["action_id"],
        }
    return {
        "blocked": (
            f"supervised action {item['action_id']} remains uncertain: "
            f"{result.get('reason', 'reconciliation required')}"
        ),
        "last_result": "uncertain_action_pending",
        "action_id": item["action_id"],
    }


def _arm_stage_usage_sidecar(
    repo: Path, plan_name: str, run_id: str, cid: str, stage: str, round_num: int,
) -> tuple[Path | None, dict[str, str | None]]:
    """Create a per-attempt usage sidecar and export its OPSX_* environment.

    Returns ``(sidecar_path, saved_env)``; ``saved_env`` must be handed to
    :func:`_restore_stage_usage_sidecar` after the dispatch so the orchestrator
    environment is left exactly as it was. Each saved value records the
    variable's prior presence: a variable that was absent is saved as ``None``
    and removed again on restore, rather than being materialized as an empty
    string. A no-op returning ``(None, {})`` when the plan name or run id is
    unavailable.
    """
    if not (plan_name and run_id):
        return None, {}
    sidecar_path = _build_usage_sidecar_path(repo, plan_name, cid, stage, round_num)
    sidecar_path.parent.mkdir(parents=True, exist_ok=True)
    extra_env = _build_usage_sidecar_env(
        plan_name, run_id, cid, stage, round_num, sidecar_path
    )
    saved_env: dict[str, str | None] = {}
    for key, value in extra_env.items():
        saved_env[key] = os.environ.get(key)
        os.environ[key] = value
    return sidecar_path, saved_env


def _restore_stage_usage_sidecar(saved_env: dict[str, str | None]) -> None:
    for key, value in saved_env.items():
        if value is None:
            os.environ.pop(key, None)
        else:
            os.environ[key] = value


def _restore_direct_usage_sidecar(sidecar_state: dict) -> None:
    """Restore the OPSX_* environment armed by the direct change loop.

    Called from a ``finally`` so every exit from the loop — including a
    budget-gate early return on a blocked reservation — restores the
    orchestrator environment exactly. A variable that was absent before the
    arm is removed again rather than materialized as an empty string.
    """
    extra_env = sidecar_state.get("extra_env")
    if not extra_env:
        return
    saved_env = sidecar_state.get("saved_env") or {}
    for key in extra_env:
        os.environ.pop(key, None)
        if key in saved_env and saved_env[key] is not None:
            os.environ[key] = saved_env[key]


def record_create_stage_telemetry(
    repo: Path,
    cfg: dict,
    state: dict,
    cid: str,
    attempt: int,
    run_id: str,
    started_at: str,
    ended_at: str,
    outcome: str,
    log_path: Path,
    create_invoke: str,
    sidecar_path: Path | None,
) -> dict | None:
    """Write the create dispatch's telemetry record (role-attributed).

    The ``create`` stage is a supervised_author model call, so a successful
    dispatch must emit a telemetry record and be reconciled from its observed
    usage — or retained when usage is unknown. Returns the written record (or
    ``None`` when writing fails).
    """
    stage = "create"
    round_num = attempt
    duration_ms = telemetry.compute_duration_ms(started_at, ended_at)
    if outcome == "env_error":
        telemetry_status = "spawn_error"
        error_message = log_path.read_text(
            encoding="utf-8"
        ).strip().lstrip("#").strip()
    elif outcome == "spawn_error":
        telemetry_status = "spawn_error"
        error_message = f"could not spawn {stage}: {create_invoke}"
    elif outcome == "timeout":
        telemetry_status = "timeout"
        error_message = f"{stage} timed out"
    else:
        telemetry_status = "completed"
        error_message = None

    payload, _parse_why, envelope = parse_stage_json(log_path)

    try:
        return telemetry._record_stage_telemetry(
            repo, cfg, state, cid, stage, round_num,
            started_at, ended_at, duration_ms,
            telemetry_status, error_message,
            payload, log_path,
            sidecar_path=sidecar_path,
            envelope=envelope,
            role=telemetry.resolve_stage_role(stage),
            worker_command=create_invoke,
        )
    except Exception as exc:
        base.log(
            f"warning: failed to write telemetry for {cid}/{stage} "
            f"r{round_num}: {exc}"
        )
        return None


def dispatch_create_stage(
    repo: Path,
    cfg: dict,
    state: dict,
    cid: str,
    attempt: int,
    create_invoke: str,
    r: dict,
    run_id: str = "",
    before_tracked: tuple[str, str, str] | None = None,
) -> tuple[str, Path] | dict:
    """Run one create dispatch under the durable budget gate.

    Returns ``(outcome, log_path)`` after a dispatch, or a blocked dict
    (``{"blocked": reason, "last_result": ...}``) when the supervised gate
    refuses to let the create proceed. For a registered supervised job the
    create call reserves before spawning, emits a role-attributed telemetry
    record, and reconciles the reservation from the observed usage (retaining
    it when usage is unknown). An unregistered run takes the legacy
    ``run_stage`` path untouched, with no run-id or state side effect.
    """
    try:
        gate = open_supervised_gate(repo, cfg.get("_manifest_path"))
    except SupervisionGateError as exc:
        return {
            "blocked": f"supervised gate unavailable: {exc}",
            "last_result": "supervision_gate_unavailable",
        }
    if gate is None:
        return run_stage(
            repo, cfg, cid, "create", create_invoke,
            cfg["create_timeout_minutes"], attempt,
        )
    effective_invoke = _effective_create_invoke(cfg, cid, create_invoke)
    resolved_model = _resolved_dispatch_model(
        repo, cfg, "create", r, invoke=effective_invoke
    )
    creation_baseline = (
        before_tracked
        if before_tracked is not None
        else groundtruth.tracked_worktree_snapshot(repo)
    )
    if not run_id:
        run_id = state.get("run_id", "") or telemetry.get_or_create_run_id(
            repo, cfg, state
        )
    started_at = base.utcnow()
    sidecar: Path | None = None
    saved_env: dict[str, str | None] = {}
    try:
        sidecar, saved_env = _arm_stage_usage_sidecar(
            repo, cfg.get("name", ""), run_id, cid, "create", attempt
        )
        reservation = _recover_supervised_dispatch(
            repo,
            cfg,
            gate,
            cid,
            "create",
            attempt,
            r,
            run_id,
            resolved_model,
            reobserve=lambda: groundtruth.change_authored(repo, cid),
        )
        if reservation is None:
            reservation = supervised_gated_reserve(
                repo, cfg, gate, cid, "create", attempt, r, run_id,
                resolved_model=resolved_model,
            )
        if reservation.get("recovered"):
            return reservation
        if reservation.get("blocked"):
            return {
                "blocked": reservation["blocked"],
                "last_result": reservation.get("last_result", "budget_blocked"),
            }
        try:
            outcome, log_path = run_stage(
                repo, cfg, cid, "create", create_invoke,
                cfg["create_timeout_minutes"], attempt,
            )
        except BaseException:
            _load_journal_dispatch().mark_active_uncertain(
                "create dispatch raised before its outcome was confirmed"
            )
            raise
        ended_at = base.utcnow()
        # A supervised create dispatch is role-attributed and its observed
        # usage is reconciled: the create worker is a supervised_author model
        # call and must be accounted, never silently reconciled as a zero-cost
        # observed action.
        record = record_create_stage_telemetry(
            repo, cfg, state, cid, attempt, run_id, started_at, ended_at,
            outcome, log_path, effective_invoke, sidecar,
        )
        reservation_id = reservation.get("reservation_id")
        action_id = reservation.get("action_id")
        if reservation_id is not None or action_id is not None:
            try:
                verified, verification_reason = groundtruth.verify_change_created(
                    repo, cfg, cid, creation_baseline
                )
            except BaseException:
                _load_journal_dispatch().resolve_dispatch(
                    gate,
                    action_id=action_id,
                    reservation_id=reservation_id,
                    outcome="create_verification_error",
                    record=record,
                )
                raise
            state_name = (
                "completed"
                if outcome == "exited" and verified
                else "failed" if outcome == "exited" else create_outcome_state(outcome)
            )
            _load_journal_dispatch().resolve_dispatch(
                gate,
                action_id=action_id,
                reservation_id=reservation_id,
                outcome=state_name,
                record=record,
            )
            if state_name != "completed":
                if outcome == "exited" and not verified:
                    base.log(
                        f"  create result evidence did not verify: {verification_reason}"
                    )
                supervised_gate_record_incident(
                    gate, cid, "create", discriminator="dispatch"
                )
        return outcome, log_path
    finally:
        _restore_stage_usage_sidecar(saved_env)
        close_supervised_gate(gate)


def run_direct_change(
    repo: Path,
    cfg: dict,
    state: dict,
    cid: str,
    budget_deadline: float | None = None,
    budget_usd: float = 0.0,
) -> str:
    r = state_mod.rec(state, cid)
    if r.get("last_result") == "recovered_action_state_mismatch":
        return "failed"
    # A worktree with a registered supervised job gets the durable budget gate;
    # an ordinary, unregistered run gets ``None`` and keeps the legacy gates
    # below byte-identical. A present-but-unreadable supervision backend fails
    # closed: the change blocks before dispatch rather than silently falling
    # back to the unbudgeted legacy path.
    try:
        supervised_gate = open_supervised_gate(repo, cfg.get("_manifest_path"))
    except SupervisionGateError as exc:
        reason = f"supervised gate unavailable: {exc}"
        r["last_result"] = "supervision_gate_unavailable"
        state_mod.set_status(state, cid, base.PENDING, reason)
        base.log(f"  {reason}")
        persist_direct_state(repo, cfg, state, cid)
        return "budget"
    if supervised_gate is not None:
        # Resume revalidation: before the first dispatch after a restart, pause,
        # or human wait, every relied-upon receipt is matched against the current
        # material revision. A stale receipt re-arms its gate and raises
        # StaleMaterialError into this change's incident flow instead of
        # dispatching.
        try:
            broker_mod.assert_resume_clear(
                supervised_gate["ledger"], supervised_gate["job_id"],
                change_ids=[cid],
            )
        except broker_mod.StaleMaterialError as exc:
            reason = f"stale material revision: {exc}"
            r["last_result"] = "stale_material"
            state_mod.set_status(state, cid, base.PENDING, reason)
            base.log(f"  {reason}")
            persist_direct_state(repo, cfg, state, cid)
            close_supervised_gate(supervised_gate)
            return "budget"
    try:
        return _run_direct_change_loop(
            repo, cfg, state, cid, r, budget_deadline, budget_usd, supervised_gate
        )
    finally:
        close_supervised_gate(supervised_gate)


def _run_direct_change_loop(
    repo: Path,
    cfg: dict,
    state: dict,
    cid: str,
    r: dict,
    budget_deadline: float | None,
    budget_usd: float,
    supervised_gate: dict | None,
) -> str:
    """Drive the single-change loop, restoring OPSX_* env on every exit.

    The sidecar state lives outside the loop body so a ``finally`` can restore
    the environment even when a budget-gate early return fires before a
    dispatch. This keeps the environment byte-identical after a blocked
    supervised dispatch, including variables that were initially absent.
    """
    sidecar_state: dict = {"extra_env": None, "saved_env": {}}
    try:
        return _run_direct_change_loop_inner(
            repo, cfg, state, cid, r, budget_deadline, budget_usd,
            supervised_gate, sidecar_state,
        )
    finally:
        _restore_direct_usage_sidecar(sidecar_state)


def _run_direct_change_loop_inner(
    repo: Path,
    cfg: dict,
    state: dict,
    cid: str,
    r: dict,
    budget_deadline: float | None,
    budget_usd: float,
    supervised_gate: dict | None,
    sidecar_state: dict,
) -> str:
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
        if stage == "recovery":
            # A registered supervised job drives its bounded recovery flow
            # here; an unregistered run has no gate and is driven back onto
            # the ordinary path with its behavior unchanged.
            action = drive_supervised_recovery(repo, cfg, state, cid, r, supervised_gate)
            persist_direct_state(repo, cfg, state, cid)
            if action == "continue":
                continue
            return action
        # --- supervised budget pre-dispatch gate ---
        # Active only for a registered supervised job; it replaces the legacy
        # spend gate for that job while leaving the legacy path untouched. The
        # actual reservation is taken after the escalation decision so an
        # escalated implement dispatch is reserved under its own role.
        gate_reservation: dict | None = None
        # --- spend-budget pre-dispatch check (legacy, unregistered runs only) ---
        if supervised_gate is None and budget_usd > 0:
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
        if stage not in {"implement", "review", "acceptance", "fix", "verify", "archive"}:
            r["phase"] = "implement"
            stage = "implement"
        if stage in {"acceptance", "fix", "verify"} and supervised_gate is None:
            # The acceptance stage and its fix/verify helpers are
            # supervised-only; a leftover phase from a deregistered run is
            # driven back onto the ordinary path.
            r["phase"] = "implement"
            stage = "implement"

        if stage == "acceptance":
            if not cfg.get("acceptance_invoke"):
                reason = (
                    f"acceptance stage invoke is not configured for adapter "
                    f"'{cfg.get('adapter', '')}'; refusing to skip the supervised "
                    "acceptance stage"
                )
                r["last_result"] = "acceptance_invoke_unconfigured"
                state_mod.set_status(state, cid, base.FAILED, reason)
                _try_notify(cfg, "change_failed", reason, change_id=cid)
                persist_direct_state(repo, cfg, state, cid)
                return "failed"
            prepared = prepare_acceptance_attempt(
                repo, cfg, state, cid, r, gate=supervised_gate
            )
            if prepared.get("blocked"):
                reason = prepared["blocked"]
                r["last_result"] = prepared.get(
                    "last_result", "acceptance_blocked"
                )
                blocked_status = (
                    base.PENDING if prepared.get("retryable") else base.FAILED
                )
                state_mod.set_status(state, cid, blocked_status, reason)
                base.log(f"  {reason}")
                persist_direct_state(repo, cfg, state, cid)
                return "budget" if prepared.get("retryable") else "failed"
        elif stage in {"fix", "verify"} and not cfg.get(f"{stage}_invoke"):
            reason = (
                f"{stage} stage invoke is not configured for adapter "
                f"'{cfg.get('adapter', '')}'; the acceptance repair route cannot "
                "run and is not skipped"
            )
            r["last_result"] = f"{stage}_invoke_unconfigured"
            state_mod.set_status(state, cid, base.FAILED, reason)
            _try_notify(cfg, "change_failed", reason, change_id=cid)
            persist_direct_state(repo, cfg, state, cid)
            return "failed"

        input_block = build_worker_input(repo, cfg, state, cid, stage=stage)
        state_mod.set_status(state, cid, base.RUNNING, f"{stage} round {round_num}")
        persist_direct_state(repo, cfg, state, cid)

        # 3.1 Capture started_at before invocation
        started_at = base.utcnow()
        plan_name = cfg["name"]
        run_id = telemetry.get_or_create_run_id(repo, cfg, state)

        # ---- usage sidecar (OpenCode plugin only; harmless no-op for other adapters) ----
        # The armed environment and the saved prior values live in
        # ``sidecar_state`` so the loop's caller can restore them from a
        # ``finally`` regardless of which path returns.
        sidecar_path: Path | None = None
        extra_env: dict[str, str] | None = None

        def _arm_usage_sidecar() -> None:
            """Create a fresh per-attempt sidecar and export its OPSX_* env."""
            nonlocal sidecar_path, extra_env
            if not (plan_name and run_id):
                return
            sidecar_path = _build_usage_sidecar_path(repo, plan_name, cid, stage, round_num)
            sidecar_path.parent.mkdir(parents=True, exist_ok=True)
            extra_env = _build_usage_sidecar_env(plan_name, run_id, cid, stage, round_num, sidecar_path)
            saved_env = sidecar_state["saved_env"]
            for key, value in extra_env.items():
                if key not in saved_env:
                    saved_env[key] = os.environ.get(key)
                os.environ[key] = value
            sidecar_state["extra_env"] = extra_env

        def _restore_usage_sidecar() -> None:
            _restore_direct_usage_sidecar(sidecar_state)

        _arm_usage_sidecar()

        def _write_telemetry(telemetry_status: str, error_message: str | None) -> None:
            """Write a telemetry record. Logs a warning on failure; never raises.

            Returns the written record (or ``None`` on failure) so the
            supervised gate can reconcile observed usage from the definitive
            record.
            """
            try:
                return telemetry._record_stage_telemetry(
                    repo, cfg, state, cid, stage, round_num,
                    started_at, ended_at, duration_ms,
                    telemetry_status, error_message,
                    payload, log_path,
                    sidecar_path=sidecar_path,
                    envelope=envelope,
                    role=telemetry.resolve_stage_role(
                        stage,
                        escalation_active=bool(
                            r.get("escalation", {}).get("active")
                        ),
                    ),
                )
            except Exception as exc:
                base.log(f"warning: failed to write telemetry for {cid}/{stage} r{round_num}: {exc}")
                return None

        def _reconcile_supervised(outcome: str, record: dict | None) -> dict | None:
            """Resolve the current action after a dispatch attempt.

            Returns the bounded-recovery result for a failing outcome so the
            caller can route the change into the driven recovery phase; a
            clean outcome (or an unregistered run) returns ``None``.
            """
            if supervised_gate is None:
                return None
            entry = gate_reservation or {}
            reservation_id = entry.get("reservation_id")
            action_id = entry.get("action_id")
            if reservation_id is None and action_id is None:
                return None
            _load_journal_dispatch().resolve_dispatch(
                supervised_gate,
                action_id=action_id,
                reservation_id=reservation_id,
                outcome=outcome,
                record=record,
            )
            if outcome == "invalid_output" and action_id is not None:
                # An invalid structured result is a definitive judgment, not
                # an interrupted observation: the worker exited and its output
                # was parsed and found unusable. Resolve the action to failed
                # so it cannot linger uncertain and block the bounded retry or
                # the recovery redispatch behind a nonexistent uncertainty.
                _load_journal_dispatch().fail_judged_action(
                    supervised_gate,
                    action_id=action_id,
                    detail="invalid structured worker output",
                )
            recovery = None
            # A non-clean outcome is one failing attempt at this stage's stable
            # signature; a clean outcome does not consume the bound.
            if outcome not in ("completed", "exited"):
                supervised_gate_record_incident(
                    supervised_gate, cid, stage, discriminator="dispatch"
                )
                # Route the failed supervised dispatch into bounded recovery:
                # classify it, link the incident to its signature, and record
                # any primary-chosen remedy. An unregistered run has no gate
                # and consults no recovery code.
                recovery = begin_supervised_recovery(
                    supervised_gate,
                    cid,
                    stage,
                    dispatch_failure_evidence(outcome, record),
                    run_id=state.get("run_id", ""),
                )
            # The reconciled record is current; clear the pending action so a
            # retry within this stage cannot reconcile the same action twice.
            entry["reservation_id"] = None
            entry["action_id"] = None
            return recovery

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
        recovered_stage = False
        recovery_rerouted = False
        while True:
            # ---- supervised budget pre-dispatch gate ----
            # Reserve before any dispatch side effect. A reserve that cannot
            # be durably written, a known exhaustion, an unknown price, a
            # bounded attempt, or a legacy policy blocks the dispatch rather
            # than running unaccounted. Each dispatch attempt (including an
            # invalid-output retry) is its own action and gets its own
            # reservation.
            if supervised_gate is not None:
                run_id_for_gate = state.get("run_id", "") or telemetry.get_or_create_run_id(
                    repo, cfg, state
                )
                resolved_model = _resolved_dispatch_model(repo, cfg, stage, r)
                gate_reservation = _recover_supervised_dispatch(
                    repo,
                    cfg,
                    supervised_gate,
                    cid,
                    stage,
                    round_num,
                    r,
                    run_id_for_gate,
                    resolved_model,
                    reobserve=lambda: _reobserve_direct_stage_completion(
                        repo, cid, stage, r
                    ),
                )
                if gate_reservation is None:
                    gate_reservation = supervised_gated_reserve(
                        repo, cfg, supervised_gate, cid, stage, round_num, r,
                        run_id_for_gate, resolved_model=resolved_model,
                    )
                if gate_reservation.get("recovered"):
                    _restore_usage_sidecar()
                    recovered_stage = True
                    break
                if gate_reservation.get("blocked"):
                    reason = gate_reservation["blocked"]
                    r["last_result"] = gate_reservation.get(
                        "last_result", "budget_blocked"
                    )
                    if r["last_result"] == "uncertain_action_pending":
                        # An interrupted action could not be reconciled from
                        # evidence: a registered job routes the process
                        # interruption into the driven recovery phase, which
                        # reconciles the recorded action through the journal
                        # evidence boundary and resumes only after decisive
                        # reconciliation. A bounded escalation fails the
                        # change terminally. The registered-job guard is
                        # inside the helper; an unregistered run keeps the
                        # legacy pending-budget halt below.
                        recovery = begin_supervised_recovery(
                            supervised_gate,
                            cid,
                            stage,
                            {
                                "failure_class": "process_interruption",
                                "message": reason,
                            },
                            run_id=state.get("run_id", ""),
                        )
                        if (
                            isinstance(recovery, dict)
                            and recovery.get("status") == "recovering"
                        ):
                            r["recovery"] = {
                                "incident_id": recovery.get("incident_id"),
                                "failure_class": recovery.get("failure_class"),
                                "signature": recovery.get("signature"),
                                "origin_stage": stage,
                                "summary": reason,
                                # The specific interrupted action this
                                # incident reconciles before any resume.
                                "action_id": gate_reservation.get("action_id"),
                            }
                            r["phase"] = "recovery"
                            state_mod.set_status(
                                state, cid, base.PENDING,
                                f"{reason}; bounded incident recovery is pending",
                            )
                            base.log(
                                f"  {reason}; routed to bounded incident "
                                f"recovery (incident "
                                f"{recovery.get('incident_id')})"
                            )
                            persist_direct_state(repo, cfg, state, cid)
                            recovery_rerouted = True
                            break
                        if (
                            isinstance(recovery, dict)
                            and recovery.get("status") == "escalated"
                        ):
                            r["last_result"] = "recovery_escalated"
                            state_mod.set_status(state, cid, base.FAILED, reason)
                            _try_notify(
                                cfg, "change_failed", reason, change_id=cid
                            )
                            base.log(f"  {reason}")
                            persist_direct_state(repo, cfg, state, cid)
                            return "failed"
                    blocked_status = (
                        base.FAILED
                        if r["last_result"] == "recovered_action_state_mismatch"
                        else base.PENDING
                    )
                    state_mod.set_status(state, cid, blocked_status, reason)
                    base.log(f"  {reason}")
                    persist_direct_state(repo, cfg, state, cid)
                    return (
                        "failed"
                        if blocked_status == base.FAILED
                        else "budget"
                    )

            try:
                outcome, log_path = invoke_direct_stage(
                    repo, cfg, cid, stage, round_num, attempt_input
                )
            except BaseException:
                if supervised_gate is not None:
                    _load_journal_dispatch().mark_active_uncertain(
                        f"{stage} dispatch raised before its outcome was confirmed"
                    )
                raise

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
                telemetry_record = _write_telemetry("spawn_error", reason)
                _reconcile_supervised("env_error", telemetry_record)
                state_mod.rec(state, cid)["last_result"] = f"{stage}_env_error"
                state_mod.set_status(state, cid, base.FAILED, reason)
                _try_notify(cfg, "change_failed", reason, change_id=cid)
                persist_direct_state(repo, cfg, state, cid)
                return "spawn_error"

            if outcome == "spawn_error":
                telemetry_record = _write_telemetry(
                    "spawn_error",
                    f"could not spawn {stage}: {cfg[f'{stage}_invoke']}",
                )
                _reconcile_supervised("spawn_error", telemetry_record)
                state_mod.rec(state, cid)["last_result"] = f"{stage}_spawn_error"
                state_mod.set_status(state, cid, base.FAILED, f"could not spawn {stage}: {cfg[f'{stage}_invoke']}")
                _try_notify(cfg, "change_failed", f"could not spawn {stage}", change_id=cid)
                persist_direct_state(repo, cfg, state, cid)
                return "spawn_error"

            if outcome == "timeout":
                telemetry_record = _write_telemetry("timeout", f"{stage} timed out")
                recovery = _reconcile_supervised("timeout", telemetry_record)
                state_mod.rec(state, cid)["last_result"] = f"{stage}_timeout"
                if isinstance(recovery, dict) and recovery.get("status") == "recovering":
                    # A registered job's transient dispatch failure is routed
                    # to the driven recovery phase (bounded retry) instead of
                    # turning terminal; legacy runs keep the terminal halt.
                    r["recovery"] = {
                        "incident_id": recovery.get("incident_id"),
                        "failure_class": recovery.get("failure_class"),
                        "signature": recovery.get("signature"),
                        "origin_stage": stage,
                        "summary": f"{stage} timed out",
                    }
                    r["phase"] = "recovery"
                    state_mod.set_status(
                        state, cid, base.PENDING,
                        f"{stage} timed out; bounded incident recovery is pending",
                    )
                    persist_direct_state(repo, cfg, state, cid)
                    recovery_rerouted = True
                    break
                state_mod.set_status(state, cid, base.FAILED, f"{stage} timed out")
                _try_notify(cfg, "change_failed", f"{stage} timed out", change_id=cid)
                persist_direct_state(repo, cfg, state, cid)
                return "failed"

            payload, parse_why, envelope = parse_stage_json(log_path)
            if payload is not None:
                break
            if _is_retriable_invalid_output(parse_why) and invalid_attempt < invalid_retries_max:
                invalid_attempt += 1
                telemetry_record = _write_telemetry("invalid_output", parse_why)
                _reconcile_supervised("invalid_output", telemetry_record)
                base.log(
                    f"  {stage} round {round_num}: output invalid ({parse_why}); "
                    f"retrying ({invalid_attempt}/{invalid_retries_max})"
                )
                # Re-arm a fresh usage sidecar for the retry attempt.
                _arm_usage_sidecar()
                attempt_input = input_block + "\n" + _INVALID_OUTPUT_RETRY_HINT
                continue
            telemetry_record = _write_telemetry("invalid_output", parse_why)
            recovery = _reconcile_supervised("invalid_output", telemetry_record)
            state_mod.rec(state, cid)["last_result"] = "subagent_output_invalid"
            # The dispatch's built-in retries are exhausted: a registered job
            # is routed to the driven recovery phase (fixer repair plus
            # independent verification) and fails only when recovery escalates
            # or its bound is exhausted. Legacy runs are untouched.
            if isinstance(recovery, dict) and recovery.get("status") == "recovering":
                r["recovery"] = {
                    "incident_id": recovery.get("incident_id"),
                    "failure_class": recovery.get("failure_class"),
                    "signature": recovery.get("signature"),
                    "origin_stage": stage,
                    "summary": f"{stage} output invalid: {parse_why}",
                }
                r["phase"] = "recovery"
                state_mod.set_status(
                    state, cid, base.PENDING,
                    f"{stage} output invalid: {parse_why}; "
                    "bounded incident recovery is pending",
                )
                persist_direct_state(repo, cfg, state, cid)
                recovery_rerouted = True
                break
            if stage == "archive":
                state_mod.rec(state, cid)["archive"]["status"] = "failed"
                state_mod.rec(state, cid)["archive"]["reason"] = parse_why
            state_mod.set_status(state, cid, base.FAILED, f"{stage} output invalid: {parse_why}")
            _try_notify(cfg, "change_failed", f"{stage} output invalid", change_id=cid)
            persist_direct_state(repo, cfg, state, cid)
            return "failed"

        if recovery_rerouted:
            continue

        if recovered_stage:
            continue

        # Parseable payload: apply control-flow dispatch first, then record
        # telemetry with the definitive outcome.
        if stage == "implement":
            action = apply_implement_result(repo, cfg, state, cid, payload)
        elif stage == "review":
            action = apply_review_result(
                repo, cfg, state, cid, payload,
                supervised=supervised_gate is not None,
                gate=supervised_gate,
            )
        elif stage == "acceptance":
            action = apply_acceptance_result(
                repo, cfg, state, cid, payload,
                gate=supervised_gate,
                action_id=(gate_reservation or {}).get("action_id"),
            )
        elif stage == "fix":
            action = apply_fix_result(
                repo, cfg, state, cid, payload,
                action_id=(gate_reservation or {}).get("action_id"),
            )
        elif stage == "verify":
            action = apply_verify_result(
                repo, cfg, state, cid, payload,
                gate=supervised_gate,
                action_id=(gate_reservation or {}).get("action_id"),
            )
        else:
            action = apply_archive_result(
                repo, cfg, state, cid, payload,
                supervised=supervised_gate is not None,
                gate=supervised_gate,
            )
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

        telemetry_record = _write_telemetry(telemetry_status, error_message)
        _reconcile_supervised(
            "completed" if telemetry_status == "completed" else telemetry_status,
            telemetry_record,
        )

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

    repo_supervisor = repo / "lib" / "supervisor"
    if repo_supervisor.is_dir():
        installed_supervisor = (
            Path.home() / ".local" / "lib" / "opsx-controller" / "lib" / "supervisor"
        )
        stale_reason = _diff_supervisor_package(repo_supervisor, installed_supervisor)
        if stale_reason:
            return (False, label, stale_reason)

    return (True, label, "")


def _diff_supervisor_package(repo_pkg: Path, installed_pkg: Path) -> str:
    """Compare the installed lib.supervisor tree against the repo copy.

    Returns a non-empty reason string when a module (including
    ``model_policy.py``) differs, is missing, or exists only in the installed
    copy; returns "" when they match.
    """
    import hashlib

    if not installed_pkg.is_dir():
        return (
            "Installed lib.supervisor runtime package is missing; rerun a "
            "global installer"
        )

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
        return "Installed lib.supervisor runtime package is stale; rerun a global installer"
    return ""


def run_doctor_checks(repo: Path, plan_src: str | None,
                      adapter: str = "opencode", cfg: dict | None = None) -> int:
    """Run all doctor preflight checks. Returns count of failures."""
    checks: list[tuple[bool, str, str]] = []

    # Plan-independent checks
    checks.append(_check_stale_install(repo))
    checks.append(doctor._check_model_resolution(repo, adapter))
    checks.append(doctor._check_model_identifier_syntax(repo, adapter))
    checks.append(doctor._check_supervised_models(repo, adapter))
    checks.append(doctor._check_model_pricing_resolution(repo, adapter))
    checks.append(doctor._check_openspec_on_path(repo))
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
        if label == "Supervised model configuration is reported":
            doctor._print_supervised_model_detail(repo, adapter)
        if label == "Model pricing resolves for configured roles":
            doctor._print_model_pricing_detail(repo, adapter)

    return failures


def run_preflight_warnings(repo: Path, plan_src: str | None,
                           adapter: str = "opencode", cfg: dict | None = None) -> None:
    """Run the same checks as doctor but emit warnings without changing outcome."""
    checks: list[tuple[bool, str, str]] = []

    checks.append(_check_stale_install(repo))
    checks.append(doctor._check_model_resolution(repo, adapter))
    checks.append(doctor._check_model_identifier_syntax(repo, adapter))
    checks.append(doctor._check_supervised_models(repo, adapter))
    checks.append(doctor._check_model_pricing_resolution(repo, adapter))
    checks.append(doctor._check_openspec_on_path(repo))
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
    (outcome, log_path) where outcome is 'env_error', 'exited',
    'timeout', or 'spawn_error'. Output goes to a log file so it can be
    tailed live; the exit code is informational only."""
    log_dir = repo / ".opsx-plan" / "logs"
    log_dir.mkdir(parents=True, exist_ok=True)
    log_path = log_dir / f"{cid}.{stage}{attempt}.log"

    # Substitute the three known create placeholders manually rather than via
    # str.format(): a ``${VAR}`` create reference would otherwise be parsed as
    # a format field and raise KeyError. Replacing by name leaves ``$VAR`` and
    # ``${VAR}`` environment references intact for _expand_invoke_token below.
    formatted = _effective_create_invoke(cfg, cid, invoke_tpl)
    tokens = shlex.split(formatted)
    expanded_tokens: list[str] = []
    for token in tokens:
        value, missing_var = _expand_invoke_token(token)
        if value is None:
            message = (
                f"stage invoke references unset environment variable "
                f"'{missing_var}'"
            )
            log_path.write_text(
                f"# {base.utcnow()} {stage}: {message}\n", encoding="utf-8"
            )
            base.log(f"  exec[{stage}]: aborted - {message}")
            return "env_error", log_path
        expanded_tokens.append(value)

    # Drop tokens that expanded to empty (a set-but-empty variable, e.g. an
    # optional reasoning variant) along with a preceding flag token that
    # would otherwise dangle as ``--variant ""``. Mirrors invoke_direct_stage.
    cmd: list[str] = []
    for token in expanded_tokens:
        if not token:
            if cmd and cmd[-1].startswith("-") and "=" not in cmd[-1]:
                cmd.pop()
            continue
        cmd.append(token)

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
    except (ProcessLookupError, PermissionError, subprocess.TimeoutExpired, OSError):
        pass


def handle_sigint(signum, frame):  # noqa: ARG001
    base.log("interrupted; terminating active stage process group")
    if _current_proc is not None:
        terminate_group(_current_proc)
    # An interrupted dispatch can never be left dispatched: mark its action
    # explicitly uncertain so a resume reconciles it from evidence.
    integration = journal_dispatch
    if integration is not None and integration.active_dispatch() is not None:
        integration.mark_active_uncertain("interrupted by SIGINT")
    sys.exit(130)


# ---------------------------------------------------------------------------
# Scheduling
# ---------------------------------------------------------------------------

def classify(cfg: dict, state: dict, cid: str, gate_resolver=None) -> str:
    """Computed status for reporting: includes blocked/awaiting_approval.

    For a registered supervised job the caller supplies *gate_resolver*, a
    ``change_id -> bool`` predicate backed by broker receipts; a gated change
    then consults the broker instead of ``state["approvals"]``. The default
    (``None``) keeps the legacy JSON behavior byte-identical.
    """
    c = cfg["changes"][cid]
    r = state_mod.rec(state, cid)
    if not c["enabled"]:
        return base.SKIPPED
    if r["status"] in (base.DONE, base.FAILED, base.RUNNING):
        return r["status"]
    for dep in c["depends_on"]:
        dep_status = classify(cfg, state, dep, gate_resolver)
        if dep_status in (base.FAILED, "blocked"):
            return "blocked"
        if dep_status != base.DONE:
            return base.PENDING
    if c["pause_before"]:
        if gate_resolver is not None:
            if not gate_resolver(cid):
                return "awaiting_approval"
        elif cid not in state["approvals"]:
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
def cmd_run(args: argparse.Namespace) -> int:
    repo = Path(args.repo).resolve()
    plan_src = planref.resolve_plan(repo, args.plan)
    plan_abs = planref._resolve_plan_path(repo, plan_src)
    cfg = planref.load_plan(plan_abs, repo=repo)
    cfg["_manifest_path"] = str(plan_abs)
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
    # A registered supervised job dispatches only inside the supervised
    # execution; an ordinary `opsx-plan run` is refused with the named
    # mediation error before any lock or state mutation. The registration
    # signal is the service-owned ledger lookup, never a repo-writable marker.
    try:
        supervision_registration = supervision.require_supervised_authorization(repo)
    except broker_mod.BrokerError as exc:
        print(f"error: {type(exc).__name__}: {exc}", file=sys.stderr)
        return 2
    if supervision_registration is not None:
        try:
            _assert_run_gates_resolvable(
                supervision_registration, cfg
            )
        except broker_mod.BrokerError as exc:
            print(f"error: {type(exc).__name__}: {exc}", file=sys.stderr)
            return 2
        finally:
            supervision_registration.close()
    # Acquire the worktree execution lock after plan resolution and before any
    # state mutation. The lock is released on every exit path, including the
    # SIGINT handler (a context manager's finally runs on SystemExit).
    try:
        with lock_mod.acquire(
            repo, owner=f"opsx-plan run ({cfg['name']})", owner_kind="ordinary"
        ):
            return _cmd_run_body(args, repo, plan_src, cfg)
    except lock_mod.SupervisedOwnershipError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2
    except lock_mod.LockContentionError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2
    except lock_mod.LockReleaseError as exc:
        # The flock is already released; surface the non-durable release so the
        # invocation cannot be mistaken for a clean success.
        print(f"error: {exc}", file=sys.stderr)
        return 2


def _cmd_run_body(args: argparse.Namespace, repo: Path, plan_src: str, cfg: dict) -> int:
    # Auto-activate when an explicit path was supplied (only after load_plan
    # succeeds to avoid rewriting the pointer on failed explicit runs).
    if args.plan:
        try:
            plan_abs = planref._resolve_plan_path(repo, plan_src)
            rel = str(plan_abs.relative_to(repo))
            write_active_plan(repo, rel)
            base.log(f"active plan set to: {rel}")
        except ValueError:
            pass  # plan outside repo — skip auto-activation
    state = state_mod.load_state(repo, cfg["name"])
    signal.signal(signal.SIGINT, handle_sigint)

    # A registered supervised job resolves gates through broker receipts, not
    # the JSON approvals list. The resolver is ``None`` for an unregistered
    # run, keeping legacy ``classify()`` byte-identical. An unreadable backend
    # defers to the per-dispatch budget gate, which fails closed.
    run_registration = supervision.require_supervised_authorization(repo)
    gate_resolver = (
        supervision.gate_resolver(run_registration)
        if run_registration is not None
        else None
    )
    try:
        rc = _cmd_run_body_inner(
            args, repo, plan_src, cfg, state, gate_resolver
        )
        if rc == 0 and run_registration is not None:
            # A registered job completes only from plan/archive/fast-check
            # evidence; an ordinary run never reaches this path.
            _finalize_supervised_completion(repo, cfg, run_registration)
        return rc
    finally:
        if run_registration is not None:
            run_registration.close()


def _cmd_run_body_inner(
    args: argparse.Namespace,
    repo: Path,
    plan_src: str,
    cfg: dict,
    state: dict,
    gate_resolver,
) -> int:
    validate_dsh_state_files(repo, cfg, state)
    reconcile(repo, cfg, state)
    state_mod.save_state(repo, cfg["name"], state)
    sync_direct_worker_state(repo, cfg, state)

    # --- emit one-time notifications for awaiting states ---
    notified = state.setdefault("notified_events", {})
    for cid in cfg["order"]:
        if not cfg["changes"][cid]["enabled"]:
            continue
        status = classify(cfg, state, cid, gate_resolver)
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
        return cmd_status.cmd_status_inner(cfg, state, header="dry run: planned order", repo=repo)

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
            if c not in visited and classify(cfg, state, c, gate_resolver) in create_only_ok
        ]
        if args.only:
            ready = [c for c in ready if c in args.only]
        if not ready:
            # --- emit one-time notifications for any change newly awaiting input ---
            for cid in cfg["order"]:
                if not cfg["changes"][cid]["enabled"]:
                    continue
                status = classify(cfg, state, cid, gate_resolver)
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

            # --- supervised budget pre-dispatch gate for the create stage ---
            create_result = dispatch_create_stage(
                repo, cfg, state, cid, c_attempt, change_cfg["create_invoke"],
                r, before_tracked=before_tracked,
            )
            if isinstance(create_result, dict):
                if create_result.get("recovered"):
                    ok, why = groundtruth.verify_change_created(
                        repo, cfg, cid, before_tracked
                    )
                    if ok:
                        r["created_by_orchestrator"] = True
                        state_mod.set_status(
                            state, cid, base.PENDING, "created and verified"
                        )
                        base.log(f"  created: {cid} (recovered)")
                    else:
                        state_mod.set_status(
                            state, cid, base.FAILED,
                            f"recovered create did not verify: {why}",
                        )
                    state_mod.save_state(repo, cfg["name"], state)
                    continue
                reason = create_result["blocked"]
                r["last_result"] = create_result.get(
                    "last_result", "budget_blocked"
                )
                blocked_status = (
                    base.FAILED
                    if r["last_result"] == "recovered_action_state_mismatch"
                    else base.PENDING
                )
                state_mod.set_status(state, cid, blocked_status, reason)
                base.log(f"  {reason}")
                state_mod.save_state(repo, cfg["name"], state)
                # A budget-gated create is a terminal-for-this-run blocker, not
                # a create retry: mark the change visited so the loop stops
                # driving it (it stays pending for an operator) rather than
                # consuming its create_attempts and failing it.
                visited.add(cid)
                continue
            outcome, log_path = create_result
            r["last_log"] = str(log_path)

            if outcome == "env_error":
                # A deterministic environment configuration error (unset
                # variable in create_invoke) must fail the change terminally,
                # not fall through to change-verification retries.
                why = log_path.read_text(encoding="utf-8").strip().lstrip("#").strip()
                state_mod.set_status(
                    state, cid, base.FAILED,
                    f"create environment error: {why or change_cfg['create_invoke']}",
                )
                state_mod.save_state(repo, cfg["name"], state)
                continue

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
            classify(cfg, state, cid, gate_resolver) == base.DONE
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
                return cmd_status.cmd_status_inner(cfg, state, header="run finished (PR delivery failed)", repo=repo)
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
    return cmd_status.cmd_status_inner(cfg, state, header="run finished", repo=repo)




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



# Report command: implementation lives in lib/orchestrator/report.py
# ---------------------------------------------------------------------------


# ---------------------------------------------------------------------------
# Dashboard command: implementation lives in lib/orchestrator/dashboard.py
# ---------------------------------------------------------------------------



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
        return cmd_run_one.cmd_run_one(args)

    ap = argparse.ArgumentParser(prog="opsx-plan", description=__doc__)
    ap.add_argument("--repo", default=".", help="host project root (default: cwd)")
    sub = ap.add_subparsers(dest="cmd", required=True)

    p_use = sub.add_parser("use", help="activate a plan for subsequent commands")
    p_use.add_argument("plan", help="path to plan TOML")
    p_use.set_defaults(fn=cmd_use.cmd_use)

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
    p_status.add_argument(
        "--json", action="store_true",
        help="emit the plan summary and supervision object as JSON",
    )
    p_status.set_defaults(fn=cmd_status.cmd_status)

    p_approve = sub.add_parser("approve", help="approve pause_before changes")
    p_approve.add_argument("plan", nargs="?", default=None, help="path to plan TOML")
    p_approve.add_argument("change", nargs="*")
    p_approve.add_argument(
        "--all", dest="approve_all", action="store_true",
        help="approve all changes currently awaiting approval",
    )
    p_approve.set_defaults(fn=cmd_gates.cmd_approve)

    p_accept = sub.add_parser(
        "accept", help="accept orchestrator-created changes for driving"
    )
    p_accept.add_argument("plan", nargs="?", default=None, help="path to plan TOML")
    p_accept.add_argument("change", nargs="*")
    p_accept.add_argument(
        "--all", dest="accept_all", action="store_true",
        help="accept all changes currently awaiting acceptance",
    )
    p_accept.set_defaults(fn=cmd_gates.cmd_accept)

    p_reset = sub.add_parser("reset", help="reset a failed change to pending")
    p_reset.add_argument("plan", nargs="?", default=None, help="path to plan TOML")
    p_reset.add_argument("change", nargs="*")
    p_reset.add_argument(
        "--failed", action="store_true",
        help="reset all failed changes to pending",
    )
    p_reset.set_defaults(fn=cmd_gates.cmd_reset)

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
    p_archive_plan.set_defaults(fn=cmd_archive_plan.cmd_archive_plan)

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
    p_report.add_argument(
        "--reprice", action="store_true",
        help="recompute each record's cost in memory from stored usage "
             "against the current pricing catalog (read-only; telemetry and "
             "state are not modified)",
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
    p_dashboard.add_argument(
        "--reprice", action="store_true",
        help="recompute each record's cost in memory from stored usage "
             "against the current pricing catalog (read-only; telemetry and "
             "state are not modified)",
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
    p_run_one.set_defaults(fn=cmd_run_one.cmd_run_one)

    p_doctor = sub.add_parser(
        "doctor", help="run preflight checks before a run"
    )
    p_doctor.add_argument("plan", nargs="?", default=None, help="path to plan TOML")
    p_doctor.add_argument(
        "--adapter", default=None,
        choices=list(compiler.COMPILE_CLIENTS) if compiler.COMPILE_CLIENTS else None,
        help="adapter to preflight (default: plan's adapter, or opencode when no plan)",
    )
    p_doctor.set_defaults(fn=cmd_doctor.cmd_doctor)

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
    p_models_show.set_defaults(fn=cmd_models.cmd_models_show)

    p_models_env = models_sub.add_parser(
        "env", help="print shell export statements for the four resolved variables"
    )
    p_models_env.add_argument(
        "--adapter", default=None,
        help="adapter to resolve against (default: active plan's adapter)",
    )
    p_models_env.set_defaults(fn=cmd_models.cmd_models_env)

    p_models_init = models_sub.add_parser(
        "init", help="seed ~/.config/opsx-controller/models.toml from the environment"
    )
    p_models_init.add_argument(
        "--force", action="store_true", help="overwrite an existing file"
    )
    p_models_init.set_defaults(fn=cmd_models.cmd_models_init)

    p_supervise = sub.add_parser(
        "supervise",
        help="report the operator authority backend and run its fail-closed gate",
    )
    supervise_sub = p_supervise.add_subparsers(dest="supervise_cmd", required=True)

    p_supervise_status = supervise_sub.add_parser(
        "status",
        help="read-only capability report (available / unprovisioned / unsupported)",
    )
    p_supervise_status.add_argument(
        "--json", action="store_true", help="emit the capability report as JSON"
    )
    p_supervise_status.set_defaults(fn=cmd_supervise.cmd_supervise_status)

    p_supervise_probe = supervise_sub.add_parser(
        "probe",
        help="run the fail-closed gate and the mandatory activation probe",
    )
    p_supervise_probe.set_defaults(fn=cmd_supervise.cmd_supervise_probe)

    p_supervise_serve = supervise_sub.add_parser(
        "serve",
        help="host the trusted broker endpoint surface (installs the projection writer)",
    )
    p_supervise_serve.add_argument(
        "plan", nargs="?", default=None, help="path to the plan TOML"
    )
    p_supervise_serve.add_argument(
        "--store", default=None,
        help="explicit service-owned supervision store path",
    )
    p_supervise_serve.add_argument(
        "--job-id", type=int, default=None, dest="job_id",
        help="explicit supervised job id (default: the worktree's active job)",
    )
    p_supervise_serve.add_argument(
        "--once", action="store_true",
        help="accept and dispatch at most one request, then exit",
    )
    p_supervise_serve.add_argument(
        "--timeout", type=float, default=30.0,
        help="seconds to wait for a request with --once (default: 30)",
    )
    p_supervise_serve.add_argument(
        "--primary-session", action="store_true", dest="primary_session",
        help=(
            "start or adopt the job's service-managed primary session (headless "
            "server in the worker domain, adopt-by-lookup on restart)"
        ),
    )
    p_supervise_serve.set_defaults(fn=cmd_supervise.cmd_supervise_serve)

    p_supervise_register = supervise_sub.add_parser(
        "register",
        help="record the supervised job for a plan (trust-root registration)",
    )
    p_supervise_register.add_argument(
        "plan", nargs="?", default=None, help="path to the plan TOML"
    )
    p_supervise_register.add_argument(
        "--store", default=None,
        help="explicit service-owned supervision store path",
    )
    p_supervise_register.add_argument(
        "--budget-usd", type=float, default=0.0, dest="budget_usd",
        help="total cost budget for the job",
    )
    p_supervise_register.add_argument(
        "--budget-minutes", type=float, default=None, dest="budget_minutes",
        help="total elapsed budget for the job",
    )
    p_supervise_register.add_argument(
        "--per-action-usd", type=float, default=None, dest="per_action_usd",
        help="per-action cost budget for the job",
    )
    p_supervise_register.add_argument(
        "--per-action-minutes", type=float, default=None,
        dest="per_action_minutes", help="per-action elapsed budget for the job",
    )
    p_supervise_register.add_argument(
        "--deadline-minutes", type=float, default=None, dest="deadline_minutes",
        help="execution deadline for the job",
    )
    p_supervise_register.add_argument(
        "--max-incident-attempts", type=int, default=None,
        dest="max_incident_attempts", help="bounded incident attempt count",
    )
    p_supervise_register.add_argument(
        "--primary-session", action="store_true", dest="primary_session",
        help="record the linkage for a service-managed primary session",
    )
    p_supervise_register.add_argument("--json", action="store_true")
    p_supervise_register.set_defaults(fn=cmd_supervise.cmd_supervise_register)

    p_supervise_start = supervise_sub.add_parser(
        "start", help="activate a registered job and drive the supervised run engine",
    )
    p_supervise_start.add_argument(
        "plan", nargs="?", default=None, help="path to the plan TOML"
    )
    p_supervise_start.add_argument("--store", default=None)
    p_supervise_start.add_argument("--job-id", type=int, default=None, dest="job_id")
    p_supervise_start.add_argument(
        "--no-drive", action="store_true", dest="no_drive",
        help="transition to active without driving the run engine",
    )
    p_supervise_start.set_defaults(fn=cmd_supervise.cmd_supervise_start)

    p_supervise_resume = supervise_sub.add_parser(
        "resume", help="revalidate and reactivate a paused job",
    )
    p_supervise_resume.add_argument(
        "plan", nargs="?", default=None, help="path to the plan TOML"
    )
    p_supervise_resume.add_argument("--store", default=None)
    p_supervise_resume.add_argument("--job-id", type=int, default=None, dest="job_id")
    p_supervise_resume.add_argument(
        "--no-drive", action="store_true", dest="no_drive",
        help="transition to active without driving the run engine",
    )
    p_supervise_resume.set_defaults(fn=cmd_supervise.cmd_supervise_resume)

    p_supervise_inspect = supervise_sub.add_parser(
        "inspect", help="read-only projection of a supervised job",
    )
    p_supervise_inspect.add_argument(
        "plan", nargs="?", default=None, help="path to the plan TOML"
    )
    p_supervise_inspect.add_argument("--store", default=None)
    p_supervise_inspect.add_argument("--job-id", type=int, default=None, dest="job_id")
    p_supervise_inspect.add_argument("--json", action="store_true")
    p_supervise_inspect.set_defaults(fn=cmd_supervise.cmd_supervise_inspect)

    p_supervise_watchdog = supervise_sub.add_parser(
        "watchdog",
        help=(
            "run the deterministic watchdog tick (classification and bounded "
            "reconstitution) without the execution lock or a live service"
        ),
    )
    p_supervise_watchdog.add_argument(
        "plan", nargs="?", default=None, help="path to the plan TOML"
    )
    p_supervise_watchdog.add_argument("--store", default=None)
    p_supervise_watchdog.add_argument(
        "--job-id", type=int, default=None, dest="job_id",
        help=(
            "select the job whose report is shown at the top level; every "
            "registered non-terminal job is still evaluated and reported each "
            "tick"
        ),
    )
    p_supervise_watchdog.add_argument(
        "--once", action="store_true",
        help="evaluate exactly one tick and exit",
    )
    p_supervise_watchdog.add_argument(
        "--interval", type=float, default=5.0,
        help="seconds between ticks when not using --once (default: 5)",
    )
    p_supervise_watchdog.add_argument("--json", action="store_true")
    p_supervise_watchdog.set_defaults(fn=cmd_supervise.cmd_supervise_watchdog)

    for _verb, _help in (
        ("pause", "record the pause stop boundary (interrupt in-flight work)"),
        ("drain", "record the drain stop boundary (let in-flight work finish)"),
        ("cancel", "record the terminal cancellation for the job"),
    ):
        _parser = supervise_sub.add_parser(_verb, help=_help)
        _parser.add_argument(
            "plan", nargs="?", default=None, help="path to the plan TOML"
        )
        _parser.add_argument("--store", default=None)
        _parser.add_argument("--job-id", type=int, default=None, dest="job_id")
        _parser.add_argument("--json", action="store_true")
        _parser.set_defaults(fn=getattr(cmd_supervise, f"cmd_supervise_{_verb}"))

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
    p_logs.set_defaults(fn=cmd_logs.cmd_logs)

    args = ap.parse_args()
    try:
        return args.fn(args)
    except base.PlanError as exc:
        print(f"plan error: {exc}", file=sys.stderr)
        return 2


# The moved command modules reach the retained engine helpers (classify,
# reconcile, run_doctor_checks, ...) by resolving the entrypoint module by
# name from sys.modules at call time (design D3); they look up the fixed
# name "opsx_plan".  The entrypoint is a script, so under direct execution
# __name__ is "__main__", not "opsx_plan"; register a module carrying this
# module's live top-level names under the fixed name so those by-name
# lookups resolve in both modes.  If a test loader already registered the
# executing module under that name (its __dict__ IS this module's globals),
# keep it: it already carries the live names and any test patches applied to
# it.  Otherwise build a fresh module mirroring this module's globals.
_ENTRYPOINT_MODULE_NAME = "opsx_plan"
_existing = sys.modules.get(_ENTRYPOINT_MODULE_NAME)
if _existing is None or getattr(_existing, "__dict__", None) is not globals():
    _existing = types.ModuleType(_ENTRYPOINT_MODULE_NAME)
    _existing.__dict__.update(globals())
    sys.modules[_ENTRYPOINT_MODULE_NAME] = _existing

if __name__ == "__main__":
    sys.exit(main())
