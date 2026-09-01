"""``opsx-plan use`` command handler.

Moved verbatim from the ``orchestrator/opsx-plan.py`` entrypoint. The
entrypoint-local ``write_active_plan`` helper stays in the entrypoint and is
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
    _entry().write_active_plan(repo, rel)
    base.log(f"active plan set to: {rel}")
    print(f"Activated: {rel}")
    return 0
