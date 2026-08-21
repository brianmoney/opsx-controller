"""Doctor / preflight probes for opsx-plan.

Individual ``_check_*`` probes, plus shared constants. The two aggregators
(``run_doctor_checks`` and ``run_preflight_warnings``) stay in the entrypoint
along with the install-staleness probes (design decision D6).
"""

from __future__ import annotations

import shutil
import tomllib
from pathlib import Path

from lib.orchestrator import base, groundtruth, planref, telemetry


def _check_model_resolution(repo: Path, adapter: str) -> tuple[bool, str, str]:
    """Check that every model role resolves for *adapter*."""
    label = "Model roles resolve for the target adapter"
    try:
        from lib.models.resolver import ModelConfigError
        from lib.models.resolver import resolve as resolve_models
        from lib.models.types import ROLES

        resolved = resolve_models(adapter, repo=repo)
    except ModelConfigError as exc:
        return (False, label, str(exc))
    unresolved = [role for role in ROLES if not resolved[role].model]
    if unresolved:
        return (
            False,
            label,
            f"Unresolved role(s) for '{adapter}': {', '.join(unresolved)}; "
            f"run `opsx-plan models init` to seed a configuration file, or edit "
            f"models.toml directly",
        )
    return (True, label, "")


def _check_model_identifier_syntax(repo: Path, adapter: str) -> tuple[bool, str, str]:
    """Check that resolved model identifiers match *adapter*'s identifier syntax."""
    label = "Resolved model identifiers match adapter syntax"
    try:
        from lib.models.resolver import ModelConfigError
        from lib.models.resolver import resolve as resolve_models
        from lib.models.resolver import validate as validate_models
        from lib.models.types import ROLES

        resolved = resolve_models(adapter, repo=repo)
    except ModelConfigError:
        # Already reported by _check_model_resolution; nothing new to add here.
        return (True, label, "")
    warnings = validate_models(adapter, resolved)
    if not warnings:
        return (True, label, "")
    return (
        False,
        label,
        "; ".join(warnings) + " (edit models.toml or the ambient OPSX_*_MODEL value)",
    )


def _print_model_resolution_detail(repo: Path, adapter: str) -> None:
    """Print each role's resolved model and source under the model check line."""
    try:
        from lib.models.resolver import resolve as resolve_models
        from lib.models.types import ROLES

        resolved = resolve_models(adapter, repo=repo)
    except Exception:
        return
    for role in ROLES:
        entry = resolved[role]
        value = entry.model if entry.model else "(unresolved)"
        print(f"      {role:<12} {value}  [{entry.source}]")


def _check_openspec_on_path() -> tuple[bool, str, str]:
    """Check that openspec is on PATH."""
    label = "openspec on PATH"
    if shutil.which("openspec"):
        return (True, label, "")
    return (False, label, "Install openspec (e.g. npm install -g @openspec/cli)")


def _check_adapter_client_on_path(adapter: str) -> tuple[bool, str, str]:
    """Check that the configured adapter client executable is on PATH.

    The dsh adapter accepts either ``dsh`` or ``npx``: the pinned npx
    fallback means a real ``dsh`` binary need not be installed for dispatch.
    """
    client = base.ADAPTER_CLIENTS.get(adapter, adapter)
    label = f"{client} on PATH"
    if adapter == "dsh":
        if shutil.which("dsh") or shutil.which("npx"):
            return (True, label, "")
        return (
            False,
            label,
            "Install dsh or npx (the pinned npx fallback dispatches dsh "
            "via npx --yes @deepseek-ai/dsh@0.1.0-rc.7)",
        )
    if shutil.which(client):
        return (True, label, "")
    return (False, label, f"Install {client} or add it to PATH")


def _check_tracked_bytecode(repo: Path) -> tuple[bool, str, str]:
    """Check that no tracked __pycache__ dirs or .pyc files exist."""
    label = "No tracked __pycache__ or .pyc files"
    res = groundtruth.git(repo, "ls-files", "--", ":(glob)**/__pycache__/**", ":(glob)**/*.pyc")
    if res.returncode != 0:
        return (True, label, "")
    lines = [ln.strip() for ln in res.stdout.splitlines() if ln.strip()]
    if not lines:
        return (True, label, "")
    sample = ", ".join(lines[:3])
    if len(lines) > 3:
        sample += f"... and {len(lines) - 3} more"
    return (False, label, f"Tracked bytecode found: {sample}. Remove from version control and add to .gitignore")


def _check_tracked_tree_clean(repo: Path) -> tuple[bool, str, str]:
    """Check that the tracked tree has no uncommitted modifications."""
    label = "Tracked tree is clean"
    if groundtruth.tracked_tree_clean(repo):
        return (True, label, "")
    return (False, label, "Tracked files have uncommitted modifications; commit or stash before running unattended work")


def _check_plan_loads(repo: Path, plan_src: str | None) -> tuple[bool, str, str]:
    """Validate that the resolved plan loads successfully."""
    label = "Plan loads successfully"
    if plan_src is None:
        return (True, label, "")
    try:
        planref.load_plan(planref._resolve_plan_path(repo, plan_src), repo=repo)
        return (True, label, "")
    except base.PlanError as exc:
        return (False, label, f"Plan load failed: {exc}")
    except Exception as exc:
        return (False, label, f"Plan load error: {exc}")


def _check_pr_delivery(repo: Path, plan_src: str | None) -> tuple[bool, str, str]:
    """When plan enables pull-request delivery, require gh on PATH and a git remote."""
    label = "PR delivery prerequisites (gh + git remote)"
    if plan_src is None:
        return (True, label, "")

    plan_path = planref._resolve_plan_path(repo, plan_src)
    try:
        with open(plan_path, "rb") as fh:
            raw = tomllib.load(fh)
    except Exception:
        return (False, label, "Plan failed to load — cannot verify PR delivery prerequisites")

    plan_table = raw.get("plan", {})
    git_delivery = plan_table.get("git_delivery", {})
    legacy = (plan_table.get("delivery") or "").strip().lower()

    # Prefer git_delivery.create_pull_request; fall back to legacy delivery key
    if isinstance(git_delivery, dict) and git_delivery:
        if not git_delivery.get("create_pull_request", False):
            return (True, label, "")
    elif legacy == "pull-request":
        # Legacy format: delivery = "pull-request"
        pass
    else:
        return (True, label, "")

    if not shutil.which("gh"):
        return (False, label, "gh not on PATH; install GitHub CLI for PR delivery")

    res = groundtruth.git(repo, "remote")
    if res.returncode != 0 or not res.stdout.strip():
        return (False, label, "No git remote configured; add a remote for PR delivery")

    return (True, label, "")


_DIRECT_STAGE_AGENT_NAMES = ("opsx-implementer", "opsx-reviewer", "opsx-archiver")

_ADAPTER_INSTALLERS = {
    "opencode": "adapters/opencode/install.sh",
    "claude-code": "adapters/claude-code/install.sh",
    "dsh": "adapters/dsh/install.sh",
}


def _check_direct_worker_agents(cfg: dict | None, repo: Path | None = None) -> tuple[bool, str, str]:
    """When the resolved plan uses direct dispatch, verify the configured
    adapter's implement/review/archive worker agents are installed, in
    either a repo-local install (when *repo* is given) or the home-rooted
    one."""
    label = "Direct-dispatch worker agents installed"
    if cfg is None or not planref.is_direct_mode(cfg):
        return (True, label, "")

    adapter = cfg["adapter"]
    agent_dirs = telemetry._adapter_agent_dir(adapter, repo)
    if not agent_dirs:
        return (True, label, "")

    missing = [
        name for name in _DIRECT_STAGE_AGENT_NAMES
        if not any((agent_dir / f"{name}.md").is_file() for agent_dir in agent_dirs)
    ]
    if not missing:
        return (True, label, "")

    installer = _ADAPTER_INSTALLERS.get(adapter, f"the {adapter} adapter installer")
    return (
        False,
        label,
        f"Missing {adapter} worker agent(s): {', '.join(missing)}; "
        f"run {installer} to install them",
    )
