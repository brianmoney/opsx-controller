"""``opsx-plan approve`` / ``accept`` / ``reset`` gate command handlers.

Moved verbatim from the ``orchestrator/opsx-plan.py`` entrypoint. The three
handlers share ``resolve_changes`` and each carries an inline copy of the
same plan-positional heuristic (design D1/D2). Entrypoint-local engine
helpers (``classify``) are referenced through the ``_entry`` module object
the entrypoint publishes to this module after import (design D3).
"""

from __future__ import annotations

import argparse
import re
import sys
from pathlib import Path

from lib.orchestrator import base, groundtruth, planref
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
            if _entry().classify(cfg, state, cid) == "awaiting_approval"
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
            if _entry().classify(cfg, state, cid) == "awaiting_acceptance"
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
            if _entry().classify(cfg, state, cid) == base.FAILED
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
