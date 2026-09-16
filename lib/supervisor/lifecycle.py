"""The supervised job lifecycle: one state machine and its transition guards.

Every supervised-job transition — register, start, resume, pause, drain, and
cancel — lives here, so the operator endpoint verbs and the ``opsx-plan
supervise`` CLI handlers are thin adapters over exactly one implementation and
one transition table. The state machine is fixed by the contract:

    registered -> active -> (paused -> active)* -> completed | failed | cancelled

with ``completed``, ``failed``, and ``cancelled`` terminal.

Design rules enforced here:

- Standard library only, like every module in this package. Cross-module
  references go through the module object (never ``from ... import name``), so
  the package's import graph stays acyclic.
- Every transition is a durable ledger transaction; a refused transition
  changes nothing.
- Named failures ride one family: :class:`LifecycleError` with
  :class:`UnknownJobError`, :class:`TerminalJobError`, and
  :class:`IllegalTransitionError`. The family derives from
  :class:`lib.supervisor.broker.BrokerError` so the exact error name travels
  back over the mediated broker wire.
- Registration validates the isolation backend fail-closed through the
  existing authority gate before anything is written, and records the job, its
  protected policy at revision 1, the protected manifest snapshot, and the
  primary-session linkage configuration in the service-owned ledger. Nothing
  is written to the worktree or to JSON execution state.

Stop requests and human waits are durable ledger state, not process flags: a
``pause`` or ``drain`` records a job-scoped receipt plus an open ``stop`` wait,
and a human-only gate records an open ``human`` wait. Both survive a restart and
are observed at the dispatch boundary without acquiring the worktree execution
lock.
"""

from __future__ import annotations

import sqlite3
from pathlib import Path
from typing import Any, Mapping

from lib.supervisor import authority as authority_module
from lib.supervisor import broker as broker_module
from lib.supervisor import budgets as budgets_module
from lib.supervisor import ledger as ledger_module
from lib.supervisor import model_policy as model_policy_module

JOB_STATES = ledger_module.JOB_STATES
TERMINAL_JOB_STATES = ledger_module.TERMINAL_JOB_STATES

# The legal transition table. A target outside the source state's row is
# refused with :class:`IllegalTransitionError` and changes nothing.
LEGAL_TRANSITIONS: dict[str, tuple[str, ...]] = {
    "registered": ("active", "cancelled"),
    "active": ("paused", "completed", "failed", "cancelled"),
    "paused": ("active", "cancelled"),
    "completed": (),
    "failed": (),
    "cancelled": (),
}

# Every lifecycle verb that may change a job. ``inspect`` is deliberately absent:
# it is a read-only projection.
MUTATING_VERBS = ("start", "resume", "pause", "drain", "cancel")

# The supervised policy roles and the standard stage-to-role mapping, mirrored
# here as plain data so registration assembly stays free of lib.models.
SUPERVISED_POLICY_ROLES: tuple[str, ...] = tuple(model_policy_module.POLICY_ROLES)
STAGE_ROLE_MAP: dict[str, str] = dict(model_policy_module.STANDARD_STAGE_MAPPING)


class LifecycleError(broker_module.BrokerError):
    """Base class for supervised-lifecycle failures."""


class UnknownJobError(LifecycleError):
    """No registered supervised job exists for the requested target."""


class TerminalJobError(LifecycleError):
    """A mutating lifecycle operation targeted a terminal job."""


class IllegalTransitionError(LifecycleError):
    """The requested transition is outside the legal state machine."""


# ---------------------------------------------------------------------------
# Job lookup
# ---------------------------------------------------------------------------


def require_job(ledger: Any, job_id: int) -> sqlite3.Row:
    """Return the job row for *job_id* or raise :class:`UnknownJobError`."""
    try:
        return ledger.get_job(int(job_id))
    except ledger_module.UnknownRecordError as exc:
        raise UnknownJobError(str(exc)) from exc


def job_for_worktree(
    ledger: Any,
    worktree: Path | str,
    repository_root: Path | str | None = None,
) -> sqlite3.Row:
    """Return the worktree's current job row or raise :class:`UnknownJobError`.

    A worktree with a terminal job and no re-registration still resolves to its
    most recent terminal job, so a mutating verb targeting it is refused with
    the named terminal-job error rather than an unknown-job one.
    """
    job = ledger.find_job_by_worktree(worktree, repository_root=repository_root)
    if job is None:
        raise UnknownJobError(
            f"no supervised job is registered for worktree {worktree}"
        )
    return job


def current_job(
    ledger: Any,
    worktree: Path | str,
    repository_root: Path | str | None = None,
) -> sqlite3.Row | None:
    """Return the worktree's non-terminal job row, or ``None``."""
    job = ledger.find_job_by_worktree(worktree, repository_root=repository_root)
    if job is None or str(job["state"]) in TERMINAL_JOB_STATES:
        return None
    return job


# ---------------------------------------------------------------------------
# Guards
# ---------------------------------------------------------------------------


def _guard_mutable(job: sqlite3.Row) -> str:
    """Refuse a mutating verb against a terminal job, returning its state."""
    state = str(job["state"])
    if state in TERMINAL_JOB_STATES:
        raise TerminalJobError(
            f"job {job['id']} is {state}; it is terminal and refuses mutation"
        )
    return state


def _require_transition(job: sqlite3.Row, target: str) -> None:
    state = str(job["state"])
    if target not in LEGAL_TRANSITIONS.get(state, ()):
        raise IllegalTransitionError(
            f"illegal lifecycle transition {state} -> {target} for job {job['id']}"
        )


def _require_source(job: sqlite3.Row, expected: str, verb: str) -> None:
    """Refuse *verb* unless the job is in exactly *expected* state.

    The transition table alone is too coarse for verbs that share a target
    state (``start`` and ``resume`` both reach ``active``), so each verb names
    its required source state explicitly.
    """
    state = str(job["state"])
    if state != expected:
        raise IllegalTransitionError(
            f"illegal lifecycle transition: {verb} requires a {expected} job "
            f"(job {job['id']} is {state})"
        )


# ---------------------------------------------------------------------------
# Registration assembly
# ---------------------------------------------------------------------------


def build_model_selection(
    role_models: Mapping[str, Any],
    *,
    roles: tuple[str, ...] | None = None,
    stages: Mapping[str, str] | None = None,
) -> dict[str, Any]:
    """Build the protected model-selection payload from resolved role models.

    *role_models* maps a policy role to an exact model identifier (for example
    ``{"implementer": "openai/gpt-4o"}``). Roles with no resolved model are
    omitted, so an unresolved role never becomes a fake pin. The standard
    stage mapping is filtered to the pinned roles.
    """
    wanted = roles or SUPERVISED_POLICY_ROLES
    pinned = {
        role: str(role_models[role]).strip()
        for role in wanted
        if isinstance(role_models.get(role), str) and str(role_models[role]).strip()
    }
    if not pinned:
        raise LifecycleError(
            "registration requires at least one resolved supervised model role"
        )
    stage_map = dict(stages or STAGE_ROLE_MAP)
    mapped = {stage: role for stage, role in stage_map.items() if role in pinned}
    return {
        "version": model_policy_module.MODEL_POLICY_VERSION,
        "roles": pinned,
        "stages": mapped,
    }


def assemble_registration_policy(
    *,
    authority_config: Mapping[str, Any],
    role_models: Mapping[str, Any],
    allowlist_models: Any = (),
    allowlist_source: str = "unconfigured",
    total_cost_usd: float = 0.0,
    per_action_cost_usd: float | None = None,
    total_elapsed_minutes: float | None = None,
    per_action_elapsed_minutes: float | None = None,
    execution_deadline_minutes: float | None = None,
    max_incident_attempts: int | None = None,
) -> dict[str, Any]:
    """Assemble the protected job policy recorded at operator revision 1.

    The standing permissions are *authority_config*; the frozen model
    selection and inexpensive allowlist and the budget/deadline payloads are
    validated by the shared schemas when the ledger writes them. The manifest
    snapshot hash is intentionally a placeholder: the ledger derives it from
    the protected snapshot content at registration so the two cannot disagree.
    """
    return {
        "authority_config": dict(authority_config),
        "model_selection": build_model_selection(role_models),
        "inexpensive_allowlist": {
            "version": model_policy_module.MODEL_POLICY_VERSION,
            "models": [str(model) for model in allowlist_models],
            "source": str(allowlist_source),
        },
        "manifest_snapshot_hash": "",
        "budgets": {
            "version": budgets_module.BUDGET_SCHEMA_VERSION,
            "total_cost_usd": float(total_cost_usd),
            "per_action_cost_usd": per_action_cost_usd,
            "total_elapsed_minutes": total_elapsed_minutes,
            "per_action_elapsed_minutes": per_action_elapsed_minutes,
            "max_incident_attempts": max_incident_attempts,
        },
        "deadlines": {
            "version": budgets_module.BUDGET_SCHEMA_VERSION,
            "execution_deadline_minutes": execution_deadline_minutes,
        },
    }


def register(
    ledger: Any,
    *,
    worktree: Path | str,
    owner: str,
    operator: str,
    policy: Mapping[str, Any],
    manifest_content: str,
    run_id: str,
    repository_root: Path | str | None = None,
    owner_principal: str | None = None,
    owner_host: str | None = None,
    owner_boot_id: str | None = None,
    linkage_config: Mapping[str, Any] | None = None,
    require_backend: bool = True,
) -> int:
    """Record a supervised job and its registration record in one transaction.

    The isolation backend is validated fail-closed through the existing
    authority gate first (unless *require_backend* is ``False``, the
    provisioning/test seam), so an unsupported host records nothing. The
    manifest content must be a parseable canonical plan manifest; the ledger
    stores it with its derived hash. Returns the new job id.
    """
    if require_backend:
        # Raises UnsupportedHostError / ActivationProbeError before any write.
        authority_module.require_authority_backend()
    if not isinstance(manifest_content, str) or not manifest_content.strip():
        raise LifecycleError(
            "registration requires the canonical plan manifest content"
        )
    try:
        broker_module.parse_snapshot(manifest_content)
    except broker_module.BrokerError as exc:
        raise LifecycleError(
            f"the canonical plan manifest is not a valid protected snapshot: {exc}"
        ) from exc
    return ledger.register_job(
        run_id=run_id,
        worktree=worktree,
        repository_root=repository_root,
        owner=owner,
        policy=policy,
        operator=operator,
        manifest_content=manifest_content,
        owner_principal=owner_principal,
        owner_host=owner_host,
        owner_boot_id=owner_boot_id,
        linkage_config=linkage_config,
    )


# ---------------------------------------------------------------------------
# Transitions
# ---------------------------------------------------------------------------


def start(ledger: Any, job_id: int) -> sqlite3.Row:
    """Transition ``registered -> active``."""
    job = require_job(ledger, job_id)
    _guard_mutable(job)
    _require_source(job, "registered", "start")
    _require_transition(job, "active")
    ledger.set_job_state(int(job_id), "active")
    return ledger.get_job(int(job_id))


def resume(ledger: Any, job_id: int) -> sqlite3.Row:
    """Transition ``paused -> active`` after resume revalidation.

    :func:`lib.supervisor.broker.assert_resume_clear` runs first and raises the
    named stale-material error (re-arming the affected gate) when any relied-upon
    receipt no longer matches the current material revision; in that case the job
    stays ``paused`` and nothing is dispatched.
    """
    job = require_job(ledger, job_id)
    _guard_mutable(job)
    _require_source(job, "paused", "resume")
    _require_transition(job, "active")
    broker_module.assert_resume_clear(ledger, int(job_id))
    ledger.end_open_waits(int(job_id), kind="stop")
    ledger.set_job_state(int(job_id), "active")
    return ledger.get_job(int(job_id))


def pause(
    ledger: Any,
    job_id: int,
    *,
    authority: str = broker_module.OPERATOR,
    actor_principal: str | None = None,
    detail: str | None = None,
) -> sqlite3.Row:
    """Record a ``pause`` stop boundary, interrupt in-flight work, and pause.

    The stop request is a durable job-scoped receipt plus an open ``stop`` wait,
    written without the execution lock; in-flight actions are interrupted and
    marked ``uncertain`` for evidence reconciliation. The job enters ``paused``.
    """
    job = require_job(ledger, job_id)
    _guard_mutable(job)
    _require_source(job, "active", "pause")
    _require_transition(job, "paused")
    _interrupt_in_flight(ledger, int(job_id), detail="interrupted by pause")
    ledger.record_stop_request(
        int(job_id),
        kind="pause",
        authority=authority,
        actor_principal=actor_principal,
        detail=detail,
    )
    ledger.set_job_state(int(job_id), "paused")
    return ledger.get_job(int(job_id))


def drain(
    ledger: Any,
    job_id: int,
    *,
    authority: str = broker_module.OPERATOR,
    actor_principal: str | None = None,
    detail: str | None = None,
) -> sqlite3.Row:
    """Record a ``drain`` stop boundary.

    No new dispatch may begin, but in-flight actions run to a terminal outcome.
    The job enters ``paused`` immediately when nothing is in flight; otherwise it
    stays ``active`` under the durable hold until
    :func:`observe_stop_request` sees the in-flight work reach a terminal
    outcome (or the execution restarts and observes the request).
    """
    job = require_job(ledger, job_id)
    _guard_mutable(job)
    _require_source(job, "active", "drain")
    _require_transition(job, "paused")
    ledger.record_stop_request(
        int(job_id),
        kind="drain",
        authority=authority,
        actor_principal=actor_principal,
        detail=detail,
    )
    if not in_flight_actions(ledger, int(job_id)):
        ledger.set_job_state(int(job_id), "paused")
    return ledger.get_job(int(job_id))


def cancel(
    ledger: Any,
    job_id: int,
    *,
    authority: str = broker_module.OPERATOR,
    actor_principal: str | None = None,
    detail: str | None = None,
) -> sqlite3.Row:
    """Record the ``cancelled`` terminal state with explicit effects.

    In-flight work is disposed of under the journal's semantics: an action with
    only an intent (no side effect) is failed with a cancellation reason, a
    dispatched action whose outcome cannot be confirmed is marked ``uncertain``
    for reconciliation, and an already-uncertain action is left for evidence.
    Terminal ownership frees the worktree for a future registration, and no
    transition out of ``cancelled`` exists.
    """
    job = require_job(ledger, job_id)
    _guard_mutable(job)
    reason = detail or "cancelled by operator"
    _dispose_in_flight(ledger, int(job_id), reason=reason)
    ledger.end_open_waits(int(job_id))
    ledger.set_job_state(int(job_id), "cancelled")
    return ledger.get_job(int(job_id))


def complete(ledger: Any, job_id: int) -> sqlite3.Row:
    """Record supervised completion from already-verified evidence."""
    job = require_job(ledger, job_id)
    _guard_mutable(job)
    _require_source(job, "active", "complete")
    _require_transition(job, "completed")
    ledger.end_open_waits(int(job_id))
    ledger.set_job_state(int(job_id), "completed")
    return ledger.get_job(int(job_id))


def fail(ledger: Any, job_id: int, *, reason: str = "failed") -> sqlite3.Row:
    """Record the ``failed`` terminal state."""
    job = require_job(ledger, job_id)
    _guard_mutable(job)
    _require_transition(job, "failed")
    ledger.set_job_state(int(job_id), "failed")
    return ledger.get_job(int(job_id))


# ---------------------------------------------------------------------------
# In-flight disposition
# ---------------------------------------------------------------------------


def in_flight_actions(ledger: Any, job_id: int) -> list[Any]:
    """Return the job's non-terminal, non-uncertain in-flight actions."""
    return [
        action
        for action in ledger.list_actions(int(job_id))
        if str(action["state"]) in ("intent", "dispatched")
    ]


def _interrupt_in_flight(ledger: Any, job_id: int, *, detail: str) -> list[int]:
    """Mark every in-flight action ``uncertain`` for evidence reconciliation."""
    interrupted: list[int] = []
    for action in in_flight_actions(ledger, job_id):
        ledger.mark_uncertain(int(action["id"]), detail=detail)
        interrupted.append(int(action["id"]))
    return interrupted


def _dispose_in_flight(ledger: Any, job_id: int, *, reason: str) -> dict[str, list[int]]:
    disposition: dict[str, list[int]] = {"failed": [], "uncertain": []}
    for action in in_flight_actions(ledger, job_id):
        action_id = int(action["id"])
        if str(action["state"]) == "intent":
            # No side effect was attempted, so the intent is safely failed.
            ledger.fail_action(action_id, detail=reason)
            disposition["failed"].append(action_id)
        else:
            # A dispatched side effect may have happened; its outcome cannot be
            # confirmed, so it is reconciled from evidence rather than assumed.
            ledger.mark_uncertain(action_id, detail=f"{reason}: outcome unconfirmed")
            disposition["uncertain"].append(action_id)
    return disposition


# ---------------------------------------------------------------------------
# Stop-request observance and human waits
# ---------------------------------------------------------------------------


def observe_stop_request(ledger: Any, job_id: int) -> dict[str, Any] | None:
    """Observe a durable stop request at the dispatch boundary.

    Returns ``None`` when no stop hold is open. Otherwise returns a disposition
    record with ``dispatch_allowed=False``. A ``drain`` hold whose in-flight
    actions have not all reached a terminal outcome reports ``draining`` and the
    job stays ``active``; once they do (or for a ``pause`` hold) the job is
    recorded ``paused``. The hold survives a restart because it is ledger state.
    """
    job = require_job(ledger, job_id)
    job_id = int(job_id)
    state = str(job["state"])
    if state in TERMINAL_JOB_STATES:
        return {"disposition": state, "dispatch_allowed": False, "checkpoint": None}
    holds = ledger.open_waits(job_id, kind="stop")
    if not holds:
        return None
    hold = holds[-1]
    pending = in_flight_actions(ledger, job_id)
    if pending:
        return {
            "disposition": "draining",
            "dispatch_allowed": False,
            "checkpoint": str(hold["checkpoint"]),
            "in_flight": [int(action["id"]) for action in pending],
        }
    if state == "active":
        ledger.set_job_state(job_id, "paused")
    return {
        "disposition": "paused",
        "dispatch_allowed": False,
        "checkpoint": str(hold["checkpoint"]),
    }


def stop_request_pending(ledger: Any, job_id: int) -> bool:
    """True when a durable stop hold is open for *job_id*."""
    return bool(ledger.open_waits(int(job_id), kind="stop"))


def record_human_wait(
    ledger: Any,
    job_id: int,
    *,
    change_id: str,
    checkpoint: str,
    material_hash: str,
) -> int:
    """Record a durable human wait bound to its gate checkpoint.

    The wait is normal ledger state: it survives restarts, needs no execution
    lock, and dispatches nothing. It is ended by
    :func:`end_human_wait` (or :func:`lib.supervisor.ledger.Ledger.end_wait`)
    when the gate is satisfied.
    """
    job = require_job(ledger, job_id)
    _guard_mutable(job)
    return ledger.record_wait(
        int(job_id),
        kind="human",
        checkpoint=checkpoint,
        material_hash=material_hash,
        change_id=change_id,
    )


def end_human_wait(ledger: Any, job_id: int, *, change_id: str | None = None) -> int:
    """End open human waits for *job_id* (optionally only one change's)."""
    ended = 0
    for wait in ledger.open_waits(int(job_id), kind="human"):
        if change_id is not None and str(wait["change_id"]) != change_id:
            continue
        ledger.end_wait(int(wait["id"]))
        ended += 1
    return ended


def open_human_wait(ledger: Any, job_id: int, *, change_id: str | None = None) -> Any:
    """Return the newest open human wait for *job_id*, or ``None``."""
    waits = ledger.open_waits(int(job_id), kind="human")
    if change_id is not None:
        waits = [w for w in waits if str(w["change_id"]) == change_id]
    return waits[-1] if waits else None


__all__ = [
    "IllegalTransitionError",
    "JOB_STATES",
    "LEGAL_TRANSITIONS",
    "LifecycleError",
    "MUTATING_VERBS",
    "STAGE_ROLE_MAP",
    "SUPERVISED_POLICY_ROLES",
    "TERMINAL_JOB_STATES",
    "TerminalJobError",
    "UnknownJobError",
    "assemble_registration_policy",
    "build_model_selection",
    "cancel",
    "complete",
    "current_job",
    "drain",
    "end_human_wait",
    "fail",
    "in_flight_actions",
    "job_for_worktree",
    "observe_stop_request",
    "open_human_wait",
    "pause",
    "record_human_wait",
    "register",
    "require_job",
    "resume",
    "start",
    "stop_request_pending",
]
