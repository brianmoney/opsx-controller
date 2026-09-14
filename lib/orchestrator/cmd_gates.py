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
from lib.orchestrator import supervision as supervision_mod
from lib.supervisor import broker as broker_mod
from lib.supervisor import lock as lock_mod

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


def _registration_or_error(repo: Path):
    """Open the worktree's supervised registration, or return ``None``.

    An ordinary, unregistered worktree stays backend-free (``None``); a present
    but unreadable supervision backend raises so the caller fails closed.
    """
    return supervision_mod.open_registration(repo)


def _report_broker_error(exc: BaseException) -> None:
    """Print a named broker refusal so the operator sees the controlling error."""
    print(f"error: {type(exc).__name__}: {exc}", file=sys.stderr)


def _protected_snapshot(registration):
    """Return the protected snapshot for *registration*, or raise a broker error."""
    return broker_mod.load_protected_snapshot(
        registration.ledger, registration.job_id
    )


def _registered_changes(registration, args: list[str]) -> list[str] | None:
    """Resolve args against the protected snapshot for a registered job.

    ``P<N>`` resolves against the protected snapshot's phase values, and an
    exact id must be a snapshot member, never the repo plan. A worker editing
    the repo plan (phase, order, or membership) therefore cannot redirect a
    phase approval.
    """
    return broker_mod.selected_snapshot_changes(
        registration.ledger, registration.job_id, args
    )


def _snapshot_order(registration) -> list[str]:
    """Return the protected snapshot's change ids in declared order."""
    return broker_mod.snapshot_change_ids(_protected_snapshot(registration))


def _awaiting_approval(registration, cfg: dict, candidates=None) -> list[str]:
    """Return changes whose broker gate is an unreleased human-only gate.

    Batch forms (``--all`` and ``P<N>``) resolve against the protected snapshot
    and affect only human-only gates awaiting release; a delegated gate is
    released through the scoped service action, never by an operator approval.
    """
    if candidates is None:
        candidates = _snapshot_order(registration)
    affected: list[str] = []
    for cid in candidates:
        try:
            resolution = broker_mod.resolve_gate(registration.ledger, registration.job_id, cid)
        except broker_mod.BrokerError:
            continue
        if (
            not resolution.dispatchable
            and resolution.authority == broker_mod.HUMAN_ONLY
        ):
            affected.append(cid)
    return affected


def _awaiting_acceptance(registration, state: dict) -> list[str]:
    """Return orchestrator-created changes not yet accepted, from the snapshot.

    Membership, order, and the ``review_created`` flag come from the protected
    snapshot, never the repo-writable plan.
    """
    snapshot = _protected_snapshot(registration)
    if not broker_mod.snapshot_review_created(snapshot):
        return []
    affected: list[str] = []
    for cid in broker_mod.snapshot_change_ids(snapshot):
        record = state_mod.rec(state, cid)
        if record.get("created_by_orchestrator") and not record.get("accepted"):
            affected.append(cid)
    return affected


def _regenerate_projection(repo: Path, cfg: dict, state: dict) -> None:
    """Regenerate the JSON projection from broker state after a transaction."""
    registration = _registration_or_error(repo)
    if registration is None:
        return
    try:
        supervision_mod.persist_projection(
            repo, cfg, state, registration.ledger, registration.job_id
        )
    finally:
        registration.close()


def _mediated_approve(repo: Path, cfg: dict, state: dict, args: argparse.Namespace) -> int:
    """Operator-path approval for a registered job (broker mediated)."""
    registration = _registration_or_error(repo)
    try:
        if args.approve_all:
            affected = _awaiting_approval(registration, cfg)
            if not affected:
                print("No changes are currently awaiting approval.")
                return 0
        else:
            if not args.change:
                print("error: at least one change id is required", file=sys.stderr)
                return 2
            requested = _registered_changes(registration, args.change)
            if requested is None:
                return 2
            affected = _awaiting_approval(registration, cfg, candidates=requested)
            refused = [
                cid for cid in requested
                if cid not in affected
                and _resolved_authority(registration, cid) == broker_mod.DELEGATED
            ]
            if refused:
                raise broker_mod.BrokerMediationError(
                    f"change(s) {', '.join(refused)} resolve to delegated "
                    "authority; a delegated gate is released only through the "
                    "scoped service action, not an operator approval"
                )
            if not affected:
                print("No changes are currently awaiting approval.")
                return 0
        supervision_mod.call_operator(
            registration.job_id, "approve", change_ids=affected
        )
    finally:
        registration.close()
    _regenerate_projection(repo, cfg, state)
    for cid in affected:
        base.log(f"approved: {cid}")
    print(f"Approved: {', '.join(affected)}")
    return 0


def _resolved_authority(registration, cid: str) -> str | None:
    try:
        state = broker_mod.material_state(registration.ledger, registration.job_id, cid)
    except broker_mod.BrokerError:
        return None
    return state.authority


def _mediated_accept(repo: Path, cfg: dict, state: dict, args: argparse.Namespace) -> int:
    """Operator-path acceptance for a registered job (broker mediated)."""
    registration = _registration_or_error(repo)
    try:
        if args.accept_all:
            affected = _awaiting_acceptance(registration, state)
            if not affected:
                print("No changes are currently awaiting acceptance.")
                return 0
        else:
            if not args.change:
                print("error: at least one change id is required", file=sys.stderr)
                return 2
            affected = _registered_changes(registration, args.change)
            if affected is None:
                return 2
        had_failure = False
        accepted: list[str] = []
        for cid in affected:
            ok, why = groundtruth.verify_change_created(repo, cfg, cid)
            if not ok:
                print(f"refusing to accept {cid}: {why}", file=sys.stderr)
                had_failure = True
                continue
            accepted.append(cid)
        if accepted:
            supervision_mod.call_operator(
                registration.job_id, "accept", change_ids=accepted
            )
    finally:
        registration.close()
    if accepted:
        _regenerate_projection(repo, cfg, state)
        for cid in accepted:
            base.log(f"accepted: {cid}")
        print(f"Accepted: {', '.join(accepted)}")
    return 2 if had_failure else 0


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

    try:
        if supervision_mod.is_registered(repo):
            return _mediated_approve(repo, cfg, state, args)
    except broker_mod.BrokerError as exc:
        _report_broker_error(exc)
        return 2
    return _legacy_approve(repo, cfg, state, args)


def _legacy_approve(repo: Path, cfg: dict, state: dict, args: argparse.Namespace) -> int:
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

    try:
        if supervision_mod.is_registered(repo):
            return _mediated_accept(repo, cfg, state, args)
    except broker_mod.BrokerError as exc:
        _report_broker_error(exc)
        return 2
    return _legacy_accept(repo, cfg, state, args)


def _legacy_accept(repo: Path, cfg: dict, state: dict, args: argparse.Namespace) -> int:
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

    try:
        if supervision_mod.is_registered(repo):
            # A registered reset is a broker transaction and never acquires the
            # worktree execution lock.
            return _mediated_reset(repo, cfg, args)
    except broker_mod.BrokerError as exc:
        _report_broker_error(exc)
        return 2

    # Acquire the worktree execution lock after plan resolution and before any
    # state mutation, releasing on every exit path.
    try:
        with lock_mod.acquire(
            repo, owner=f"opsx-plan reset ({cfg['name']})", owner_kind="ordinary"
        ):
            return _cmd_reset_body(args, repo, cfg)
    except lock_mod.SupervisedOwnershipError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2
    except lock_mod.LockContentionError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2
    except lock_mod.LockReleaseError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2


def _mediated_reset(repo: Path, cfg: dict, args: argparse.Namespace) -> int:
    """Operator-path reset for a registered job (broker mediated)."""
    registration = _registration_or_error(repo)
    if registration is None:  # pragma: no cover - guarded by is_registered
        return 2
    state = state_mod.load_state(repo, cfg["name"])
    try:
        if args.failed:
            affected = [
                cid for cid in _snapshot_order(registration)
                if _entry().classify(cfg, state, cid) == base.FAILED
            ]
            if not affected:
                print("No failed changes to reset.")
                return 0
        else:
            if not args.change:
                print("error: at least one change id is required", file=sys.stderr)
                return 2
            affected = _registered_changes(registration, args.change)
            if affected is None:
                return 2
        supervision_mod.call_operator(
            registration.job_id, "reset_change", change_ids=affected
        )
    finally:
        registration.close()
    _regenerate_projection(repo, cfg, state)
    for cid in affected:
        base.log(f"reset: {cid}")
    if args.failed:
        print(f"Reset: {', '.join(affected)}")
    return 0


def _cmd_reset_body(args: argparse.Namespace, repo: Path, cfg: dict) -> int:
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
