"""``opsx-plan run-one`` command handler (also the ``opsx-run`` executable).

Moved verbatim from the ``orchestrator/opsx-plan.py`` entrypoint. The
entrypoint-local engine helpers it calls (``build_single_change_config``,
``write_single_change_manifest``, ``validate_dsh_state_files``, ``reconcile``,
``sync_direct_worker_state``, ``handle_sigint``, ``run_direct_change``) stay
in the entrypoint and are referenced through the ``_entry`` module object the
entrypoint publishes to this module after import (design D3).
"""

from __future__ import annotations

import argparse
import signal
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

    cfg = _entry().build_single_change_config(repo, change_id)
    state = state_mod.load_state(repo, cfg["name"])
    signal.signal(signal.SIGINT, _entry().handle_sigint)

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
    _entry().write_single_change_manifest(repo, change_id, cfg)

    _entry().validate_dsh_state_files(repo, cfg, state)
    _entry().reconcile(repo, cfg, state)
    state_mod.save_state(repo, cfg["name"], state)
    _entry().sync_direct_worker_state(repo, cfg, state)

    r = state_mod.rec(state, change_id)
    if r["status"] == base.DONE:
        base.log(f"{change_id} is already done")
        return 0

    base.log(f"=== {change_id} direct {cfg['adapter']} execution (round {r['round']}) ===")
    budget_usd = (
        float(args.budget_usd) if getattr(args, "budget_usd", 0) and float(args.budget_usd) > 0 else 0.0
    )
    result = _entry().run_direct_change(repo, cfg, state, change_id, budget_usd=budget_usd)

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
