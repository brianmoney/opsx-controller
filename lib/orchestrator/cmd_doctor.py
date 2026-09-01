"""``opsx-plan doctor`` command handler.

Moved verbatim from the ``orchestrator/opsx-plan.py`` entrypoint. The
entrypoint-local ``run_doctor_checks`` helper stays in the entrypoint and is
referenced through the ``_entry`` module object the entrypoint publishes to
this module after import (design D3).
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

from lib.orchestrator import base, planref

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

    failures = _entry().run_doctor_checks(repo, plan_src, adapter, cfg)

    print()
    if failures:
        print(f"{failures} check(s) failed")
        return 1
    print("All checks passed")
    return 0
