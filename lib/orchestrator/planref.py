"""Plan location and loading: the shared closure report, dashboard,
and the entrypoint's own dispatch all resolve plans through.

Depends only on `base` and the `lib.models` runtime package.
"""
from __future__ import annotations

import os
import tomllib
from pathlib import Path

from lib.models.resolver import ModelConfigError
from lib.models.resolver import resolve as resolve_models

from lib.orchestrator import base

ACTIVE_PLAN_FILENAME = "active-plan"


def load_plan(path: Path, repo: Path | None = None) -> dict:
    try:
        with open(path, "rb") as fh:
            raw = tomllib.load(fh)
    except Exception as exc:
        raise base.PlanError(f"cannot parse plan {path.name}: {exc}") from exc

    plan = raw.get("plan", {})
    changes = raw.get("changes", [])
    if not changes:
        raise base.PlanError("plan has no [[changes]] entries")

    adapter = plan.get("adapter", "opencode")
    if adapter not in base.ADAPTER_DEFAULTS and not (
        plan.get("implement_invoke")
        and plan.get("review_invoke")
        and plan.get("archive_invoke")
        and plan.get("state_file")
    ):
        raise base.PlanError(
            f"unknown adapter '{adapter}' and no stage invoke/state_file overrides given"
        )
    defaults = base.ADAPTER_DEFAULTS.get(adapter, {})

    cfg = {
        "name": plan.get("name") or path.stem,
        "adapter": adapter,
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
        "max_rounds": int(plan.get("max_rounds", 5)),
        "no_progress_limit": int(plan.get("no_progress_limit", 2)),
        "fast_checks": list(plan.get("fast_checks", [])),
        "check_timeout_minutes": float(plan.get("check_timeout_minutes", 15)),
        "require_clean_tracked": bool(plan.get("require_clean_tracked", True)),
        "escalate_after_review_fails": _parse_escalation_threshold(
            plan.get("escalate_after_review_fails", 0)
        ),
        "finding_recurrence_limit": _parse_finding_recurrence_limit(
            plan.get("finding_recurrence_limit", 0)
        ),
        "invalid_output_retries": _parse_invalid_output_retries(
            plan.get("invalid_output_retries", 2)
        ),
        "skip_warning": bool(plan.get("skip_warning", False)),
        "skip_suggestion": bool(plan.get("skip_suggestion", False)),
        # --- run-event notifications ---
        "notify_cmd": plan.get("notify_cmd", "").strip() if plan.get("notify_cmd") else "",
        # --- create stage (the /opsx-ff automation) ---
        "plan_doc": plan.get("plan_doc", ""),
        "create_invoke": plan.get("create_invoke", ""),
        "create_timeout_minutes": float(plan.get("create_timeout_minutes", 30)),
        "create_max_attempts": int(plan.get("create_max_attempts", 2)),
        "review_created": bool(plan.get("review_created", True)),
        "created_check": plan.get(
            "created_check", "openspec validate {change} --strict"
        ),
        # --- git delivery ---
        "git_delivery": _parse_git_delivery_config(plan.get("git_delivery", {})),
    }

    by_id: dict[str, dict] = {}
    for c in changes:
        cid = c.get("id")
        if not cid:
            raise base.PlanError("a [[changes]] entry is missing 'id'")
        if cid in by_id:
            raise base.PlanError(f"duplicate change id: {cid}")
        by_id[cid] = {
            "id": cid,
            "phase": c.get("phase"),
            "depends_on": list(c.get("depends_on", [])),
            "pause_before": bool(c.get("pause_before", False)),
            "enabled": bool(c.get("enabled", True)),
            "timeout_minutes": float(
                c.get("timeout_minutes", cfg["timeout_minutes"])
            ),
            "create_invoke": c.get("create_invoke", cfg["create_invoke"]),
            "create_max_attempts": int(
                c.get("create_max_attempts", cfg["create_max_attempts"])
            ),
        }

    for c in by_id.values():
        for dep in c["depends_on"]:
            if dep not in by_id:
                raise base.PlanError(f"{c['id']}: unknown dependency '{dep}'")

    cfg["order"] = topo_sort(by_id)
    cfg["changes"] = by_id

    try:
        cfg["models"] = resolve_models(adapter, repo=repo)
    except ModelConfigError as exc:
        raise base.PlanError(str(exc)) from exc

    missing = []
    for key in ("implement_invoke", "review_invoke", "archive_invoke"):
        if not cfg.get(key):
            missing.append(key)
    if missing:
        raise base.PlanError(
            f"plan '{cfg['name']}' is missing required stage invoke(s): "
            f"{', '.join(missing)}; all three direct stage invokes "
            f"(implement_invoke, review_invoke, archive_invoke) are required"
        )

    return cfg


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
        base.log(f"using plan from OPSX_PLAN: {norm}")
        return norm

    pointer = read_active_plan(repo)
    if pointer:
        plan_path = repo / pointer
        if not plan_path.is_file():
            raise base.PlanError(
                f"active plan pointer references missing file: {pointer}\n"
                f"Set a new active plan with: opsx-plan use <plan.toml>"
            )
        base.log(f"using active plan: {pointer}")
        return pointer

    raise base.PlanError(
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


def single_change_manifest_path(repo: Path, change_id: str) -> Path:
    """Path to the derived single-change manifest under .opsx-plan/plans/."""
    return repo / ".opsx-plan" / "plans" / f"run-{change_id}.toml"


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


def active_plan_pointer_path(repo: Path) -> Path:
    """Path to the active-plan pointer file under .opsx-plan/."""
    return repo / ".opsx-plan" / ACTIVE_PLAN_FILENAME


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
        raise base.PlanError(f"dependency cycle involving: {', '.join(cyclic)}")
    return order


def is_direct_mode(cfg: dict) -> bool:
    return all(
        cfg.get(name) for name in ("implement_invoke", "review_invoke", "archive_invoke")
    )


def _parse_escalation_threshold(value: object) -> int:
    """Parse ``escalate_after_review_fails`` into a non-negative integer.

    Raises ``PlanError`` naming the key on a negative value.
    """
    threshold = int(value)
    if threshold < 0:
        raise base.PlanError(
            "escalate_after_review_fails must be >= 0, "
            f"got {threshold}"
        )
    return threshold


def _parse_finding_recurrence_limit(value: object) -> int:
    """Parse ``finding_recurrence_limit`` into a non-negative integer.

    Raises ``PlanError`` naming the key on a negative value. ``0`` disables
    recurrence halting.
    """
    limit = int(value)
    if limit < 0:
        raise base.PlanError(
            "finding_recurrence_limit must be >= 0, "
            f"got {limit}"
        )
    return limit


def _parse_invalid_output_retries(value: object) -> int:
    """Parse ``invalid_output_retries`` into a non-negative integer.

    Raises ``PlanError`` naming the key on a negative value. ``0`` disables
    in-place retries, restoring the historic fail-on-first-invalid behavior.
    """
    retries = int(value)
    if retries < 0:
        raise base.PlanError(
            "invalid_output_retries must be >= 0, "
            f"got {retries}"
        )
    return retries


def _parse_git_delivery_config(raw: dict) -> dict:
    """Parse ``[plan.git_delivery]`` from a TOML plan into a validated cfg dict."""
    enabled = raw.get("enabled", False)
    if not isinstance(enabled, bool):
        enabled = False
    branch = str(raw.get("branch", "")).strip() if raw.get("branch") else ""
    base_ref = str(raw.get("base_ref", "")).strip() if raw.get("base_ref") else ""
    create_pull_request = raw.get("create_pull_request", False)
    if not isinstance(create_pull_request, bool):
        create_pull_request = False
    if create_pull_request and not enabled:
        raise base.PlanError(
            "plan.git_delivery.create_pull_request requires plan.git_delivery.enabled = true"
        )
    return {
        "enabled": enabled,
        "branch": branch,
        "base_ref": base_ref,
        "create_pull_request": create_pull_request,
    }
