"""``opsx-plan status`` command handler.

Moved verbatim from the ``orchestrator/opsx-plan.py`` entrypoint. The
entrypoint-local engine helpers (``reconcile``, ``sync_direct_worker_state``,
``validate_dsh_state_files``, ``classify``, ``single_line``) stay in the
entrypoint and are referenced through the ``_entry`` module object the
entrypoint publishes to this module after import (design D3).
"""

from __future__ import annotations

import argparse
import os
import sys
from pathlib import Path

from lib.models.resolver import ModelConfigError, resolve as resolve_models
from lib.models.types import ROLES
from lib.orchestrator import base, planref
from lib.orchestrator import state as state_mod

# Populated by the entrypoint immediately after import (design D3).
def _entry():
    """Resolve the entrypoint module at call time (design D3).

    The entrypoint is a script, not normally in sys.modules, and test
    loaders that exec it from file via spec/exec_module shadow
    sys.modules['opsx_plan'] with an empty module.  Resolving by name at
    call time (rather than holding an object captured at import) makes
    _entry().<helper> track the live top-level names of whichever entrypoint
    module is currently registered -- including test patches applied to
    the loaded module.
    """
    import sys
    return sys.modules["opsx_plan"]


def cmd_status(args: argparse.Namespace) -> int:
    repo = Path(args.repo).resolve()
    plan_src = planref.resolve_plan(repo, args.plan)
    cfg = planref.load_plan(planref._resolve_plan_path(repo, plan_src), repo=repo)
    state = state_mod.load_state(repo, cfg["name"])
    _entry().validate_dsh_state_files(repo, cfg, state)
    _entry().reconcile(repo, cfg, state)
    state_mod.save_state(repo, cfg["name"], state)
    _entry().sync_direct_worker_state(repo, cfg, state)
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
    return cmd_status_inner(cfg, state, header=header, plan_arg=plan_arg, repo=repo)


def display_order(cfg: dict) -> list[str]:
    """Phase-ascending for human reading (P0, P1, ...), with the scheduler's
    topological order as a stable tiebreaker within a phase. Changes without a
    phase sort last. cfg['order'] itself stays topological for dispatch."""
    topo_index = {cid: i for i, cid in enumerate(cfg["order"])}

    def key(cid: str) -> tuple:
        phase = cfg["changes"][cid].get("phase")
        return (phase is None, phase if phase is not None else 0, topo_index[cid])

    return sorted(cfg["order"], key=key)


def _print_model_banner(cfg: dict, repo: Path | None) -> None:
    """Print the effective per-role model banner for the plan's adapter.

    Resolves models through the same resolver the run uses, so the banner
    reflects exactly what stages will invoke (repo-local > user-global >
    [defaults] > env). Roles without a resolved model are shown as
    '<unresolved>' rather than omitted, surfacing misconfiguration at
    dry-run/status time instead of later.
    """
    adapter = cfg.get("adapter", "opencode")
    try:
        resolved = resolve_models(adapter, repo=repo)
    except ModelConfigError as exc:
        print(f"  models: <cannot resolve for adapter '{adapter}': {exc}>")
        return
    parts: list[str] = []
    for role in ROLES:
        entry = resolved.get(role)
        model = (entry.model if entry else None) or "<unresolved>"
        variant = entry.variant if entry and entry.variant else None
        parts.append(f"{role}: {model}" + (f"@{variant}" if variant else ""))
    suffix = ""
    esc = resolved.get("implementer_escalation")
    if esc and esc.model:
        suffix = f"  (escalation: {esc.model})"
    print(f"  models [{adapter}]: " + " | ".join(parts) + suffix)


def cmd_status_inner(cfg: dict, state: dict, header: str,
                     plan_arg: str | None = None,
                     repo: Path | None = None) -> int:
    print(header)
    _print_model_banner(cfg, repo)
    width = max(len(c) for c in cfg["order"])
    failed = 0
    for cid in display_order(cfg):
        status = _entry().classify(cfg, state, cid)
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
                print(f"      - {_entry().single_line(task)}")
    return 1 if failed else 0
