"""Gated, journal-integrated dispatch boundary for supervised plan runs.

This concern-named runtime module owns the one dispatch boundary every
supervised inner-stage dispatch flows through: the fixed pre-dispatch gate
order (execution lock, broker authority, immutable plan/policy freshness, model
policy, budget reservation), the journal lifecycle around the existing stage
dispatch call sites (intent, dispatch record with worker identity, terminal or
explicitly uncertain outcome), evidence recording, uncertainty reconciliation,
and deduplicating re-observant replay.

It wraps the existing ``opsx-plan run`` engine rather than introducing a new
DAG: the entrypoint's stage call sites delegate here when a supervised
registration exists, and an unregistered legacy run never enters the module.

Importing this module has no side effects, parses no arguments, spawns no
process, and touches no ``.opsx-plan/`` state.
"""

from __future__ import annotations

import json
import os
import sqlite3
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Mapping

from lib.orchestrator import base
from lib.orchestrator import cost as cost_mod
from lib.orchestrator import state as state_mod
from lib.orchestrator import supervision as supervision_mod
from lib.orchestrator import telemetry as telemetry_mod
from lib.supervisor import agent_contracts as agent_contracts_mod
from lib.supervisor import broker as broker_mod
from lib.supervisor import budgets as budget_mod
from lib.supervisor import ledger as ledger_mod
from lib.supervisor import lock as lock_mod
from lib.supervisor import model_policy as model_policy_mod

# The fixed evidence-kind vocabulary. Unknown kinds are stored verbatim and are
# never decisive for reconciliation or replay deduplication.
EVIDENCE_STAGE_RESULT = "stage_result"
EVIDENCE_USAGE = "usage"
EVIDENCE_SPAWN_LOSS = "spawn_loss"
EVIDENCE_SESSION_BINDING = "session_binding"
EVIDENCE_KINDS: tuple[str, ...] = (
    EVIDENCE_STAGE_RESULT,
    EVIDENCE_USAGE,
    EVIDENCE_SPAWN_LOSS,
    EVIDENCE_SESSION_BINDING,
)

# The four pre-dispatch gates, in the fixed order they are evaluated.
GATE_LOCK = "lock"
GATE_AUTHORITY = "authority"
GATE_STALE_MATERIAL = "stale_material"
GATE_MODEL_POLICY = "model_policy"
GATE_BUDGET = "budget"

JOURNAL_TERMINAL_STATES = ("completed", "failed")

# Confirmed worker results are terminal. Outcomes whose external effect cannot
# be established remain uncertain until recovery re-observes them.
_SUCCESSFUL_OUTCOMES = ("completed", "exited", "done")
_FAILED_OUTCOMES = ("failed", "spawn_error", "env_error")


class JournalDispatchError(Exception):
    """Base for a refusal raised at the journal dispatch boundary."""


class DispatchGateError(JournalDispatchError):
    """A pre-dispatch gate blocked before any side effect.

    ``gate`` names the failing gate (``lock``, ``authority``,
    ``stale_material``, ``model_policy``, or ``budget``) and ``last_result`` is
    the blocked-state vocabulary the run loop records.
    """

    gate = "dispatch"

    def __init__(self, reason: str, *, last_result: str | None = None) -> None:
        super().__init__(reason)
        self.reason = reason
        self.last_result = last_result or f"gate_{self.gate}_blocked"


class LockGateError(DispatchGateError):
    gate = GATE_LOCK


class AuthorityGateError(DispatchGateError):
    gate = GATE_AUTHORITY


class StaleMaterialGateError(DispatchGateError):
    gate = GATE_STALE_MATERIAL


class ModelPolicyGateError(DispatchGateError):
    gate = GATE_MODEL_POLICY


class BudgetGateError(DispatchGateError):
    gate = GATE_BUDGET


# The named pre-prompt egress gate for supervised stage-worker spawns. It is
# deliberately both a dispatch gate (so the run loop's existing conversion to a
# durable blocked state catches it) and the agent-contract
# :class:`EgressEnforcementError` (so a caller can catch the named enforcement
# failure exactly as it does at the session-bridge choke point).
class EgressGateError(DispatchGateError, agent_contracts_mod.EgressEnforcementError):
    gate = "egress"

    def __init__(
        self,
        reason: str,
        *,
        decision: "agent_contracts_mod.TransportDecision | None" = None,
        last_result: str | None = None,
    ) -> None:
        DispatchGateError.__init__(self, reason, last_result=last_result)
        self.decision = decision


# The named repair-consumption gate. It is both a dispatch gate (so the run
# loop reports a durable blocked state) and the agent-contract
# :class:`RepairConsumptionError` (so a caller can catch the named refusal).
# Every repair-consuming transition — resume and dispatch alike — runs it.
class RepairGateError(DispatchGateError, agent_contracts_mod.RepairConsumptionError):
    gate = "repair"

    def __init__(
        self,
        reason: str,
        *,
        decision: Mapping[str, Any] | None = None,
        last_result: str | None = None,
    ) -> None:
        DispatchGateError.__init__(self, reason, last_result=last_result)
        self.decision = dict(decision or {})


# The pricing boundary lives in :mod:`lib.orchestrator.cost` (the low layer that
# owns catalog resolution). The boundary re-exports its named retryable-catalog
# error so callers keep catching one class, and so this module never becomes a
# dependency of the pricing layer.
RetryableCatalogLoadError = cost_mod.RetryableCatalogLoadError


# The single in-flight supervised dispatch, if any. The subprocess stage
# invocation reports the spawned worker identity here; the boundary binds it to
# the action's dispatch row. Legacy runs never set it.
_active_dispatch: dict[str, Any] | None = None


# ---------------------------------------------------------------------------
# Process identity
# ---------------------------------------------------------------------------


def serialize_process_identity(pid: int) -> str:
    """Serialize a worker process identity as pid + start time + boot identity."""
    pid = int(pid)
    process_start = lock_mod.process_start_time(pid)
    boot_id = lock_mod.boot_identity()
    if pid <= 0 or process_start is None or not boot_id:
        raise JournalDispatchError(
            f"worker process {pid} has no fenceable start/boot identity"
        )
    return json.dumps(
        {
            "pid": pid,
            "process_start": process_start,
            "boot_id": boot_id,
        },
        sort_keys=True,
    )


def parse_process_identity(value: Any) -> dict[str, Any] | None:
    """Decode a serialized process identity, or ``None`` when malformed."""
    if isinstance(value, Mapping):
        decoded = dict(value)
    else:
        if not isinstance(value, str) or not value:
            return None
        try:
            decoded = json.loads(value)
        except (TypeError, ValueError):
            return None
    if not isinstance(decoded, dict):
        return None
    try:
        pid = int(decoded.get("pid"))
        process_start = float(decoded.get("process_start"))
    except (TypeError, ValueError):
        return None
    boot_id = decoded.get("boot_id")
    if pid <= 0 or not isinstance(boot_id, str) or not boot_id:
        return None
    return {"pid": pid, "process_start": process_start, "boot_id": boot_id}


def begin_active_dispatch(ledger: Any, job_id: int, action_id: int) -> None:
    global _active_dispatch
    if _active_dispatch is not None:
        raise JournalDispatchError(
            f"action {_active_dispatch['action_id']} is already the active dispatch"
        )
    _active_dispatch = {
        "ledger": ledger,
        "job_id": int(job_id),
        "action_id": int(action_id),
    }


def end_active_dispatch(action_id: int | None = None) -> None:
    global _active_dispatch
    if _active_dispatch is None:
        return
    if action_id is not None and _active_dispatch.get("action_id") != int(action_id):
        return
    _active_dispatch = None


def active_dispatch() -> dict[str, Any] | None:
    return _active_dispatch


def note_spawned_process(pid: int) -> None:
    """Bind the spawned worker process identity to the active dispatch row.

    Called by the subprocess dispatch path immediately after the worker is
    spawned. A no-op when no supervised dispatch is active (the legacy path),
    so an unregistered run never touches the supervisor journal.
    """
    context = _active_dispatch
    if context is None:
        return
    try:
        pid = int(pid)
    except (TypeError, ValueError) as exc:
        raise JournalDispatchError("spawned worker pid is invalid") from exc
    context["ledger"].bind_dispatch_identity(
        context["action_id"], process_id=serialize_process_identity(pid)
    )


def mark_active_uncertain(detail: str = "interrupted") -> bool:
    """Mark the active dispatch uncertain when its outcome cannot be confirmed.

    Routes through :func:`resolve_dispatch` so an interrupted dispatch is
    resolved exactly like any other unconfirmed outcome — never left
    dispatched.
    """
    context = _active_dispatch
    if context is None:
        return False
    gate = {"ledger": context["ledger"], "job_id": context["job_id"]}
    result = resolve_dispatch(
        gate, action_id=context["action_id"], outcome=detail, record=None
    )
    return result == "uncertain"


# ---------------------------------------------------------------------------
# Pricing / reservation estimate (orchestrator boundary)
# ---------------------------------------------------------------------------
#
# The pricing helpers live in :mod:`lib.orchestrator.cost` — the low layer that
# already owns catalog resolution — so this module (which the supervision
# service imports) never has to be imported back by it. These thin re-exports
# keep the boundary's public surface stable.


pinned_model_for_role = cost_mod.pinned_model_for_role


reservation_estimate_for_dispatch = cost_mod.reservation_estimate_for_dispatch


RetryableCatalogLoadError = cost_mod.RetryableCatalogLoadError


def execution_elapsed_minutes(ledger: Any, job_id: int) -> float:
    """Accumulate execution-elapsed minutes from ledger dispatch intervals."""
    total = 0.0
    for interval in ledger.dispatch_intervals(job_id):
        started = _parse_iso(interval["started_at"])
        ended = _parse_iso(interval["ended_at"]) if interval["ended_at"] else None
        if started is None:
            continue
        if ended is None:
            ended = datetime.now(timezone.utc)
        total += max(0.0, (ended - started).total_seconds() / 60.0)
    return total


def _parse_iso(value: Any) -> datetime | None:
    if not isinstance(value, str) or not value:
        return None
    try:
        parsed = datetime.fromisoformat(value)
    except (TypeError, ValueError):
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed


# ---------------------------------------------------------------------------
# Pre-dispatch gates
# ---------------------------------------------------------------------------


def evaluate_gates(
    repo: Path,
    gate: Mapping[str, Any],
    *,
    cid: str,
    stage: str,
    role: str,
    resolved_model: str | None = None,
    environ: Mapping[str, str] | None = None,
) -> agent_contracts_mod.TransportDecision:
    """Evaluate the fixed gate order, raising the named gate error on refusal.

    Order: execution lock held, broker authority revalidated per action,
    immutable plan/policy freshness against the registration anchors, model
    policy for the stage role, the repair-consumption gate, then the named
    pre-prompt egress enforcement. The budget reservation gate is the reserve
    mechanics that run immediately after, so a caller that has not yet reserved
    has not passed the boundary.

    Returns the enforced transport decision so the caller can journal it as
    action evidence before the dispatch record.
    """
    ledger = gate["ledger"]
    job_id = int(gate["job_id"])
    policy = gate["policy"]
    assert_lock_gate(repo, ledger, job_id)
    assert_authority_gate(ledger, job_id, cid)
    assert_material_freshness(ledger, job_id, gate)
    assert_model_policy_gate(policy, role, resolved_model=resolved_model)
    assert_repair_gate(ledger, job_id, cid, transition="dispatch")
    return assert_egress_gate(ledger, job_id, environ=environ)


def assert_repair_gate(
    ledger: Any,
    job_id: int,
    cid: str,
    *,
    transition: str = "dispatch",
    fixer_report: Mapping[str, Any] | None = None,
    verifier_verdict: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    """Require any recorded repair to be independently verified before it is consumed.

    A resume or dispatch that consumes a repair reads the change's latest
    recorded fixer report and verifier verdict from the journal (unless the
    caller presents them) and refuses with the named :class:`RepairGateError`
    — recording a durable ``policy_violation`` — when the repair is not
    independently verified. A change with no recorded repair passes: the gate
    refuses non-independent repairs, not ordinary transitions.
    """
    try:
        return agent_contracts_mod.assert_repair_consumable(
            ledger,
            int(job_id),
            transition=transition,
            change_id=cid,
            fixer_report=fixer_report,
            verifier_verdict=verifier_verdict,
        )
    except agent_contracts_mod.RepairConsumptionError as exc:
        raise RepairGateError(
            str(exc), decision=getattr(exc, "decision", None)
        ) from exc


def assert_lock_gate(repo: Path, ledger: Any, job_id: int) -> None:
    """Require the worktree execution lock to be held by this process.

    The authoritative, unforgeable proof is the service-owned
    supervised-execution fence: the job's live ledger fencing record must name
    this process (or an ancestor). A repo-writable ``.opsx-plan`` lock record
    is additionally consulted so a foreign live holder fails closed, but it is
    never accepted as authorization on its own.
    """
    reason = supervision_mod.execution_boundary_reason(ledger, job_id, pid=os.getpid())
    if reason is not None:
        raise LockGateError(
            "the worktree execution lock is not held by this process: " + reason
        )
    record = lock_mod.read_record(repo)
    if isinstance(record, dict) and record.get("state") == lock_mod.HELD:
        if lock_mod.holder_is_live(record) and lock_mod.record_identity(
            record
        ) != lock_mod.record_identity(lock_mod.current_identity()):
            raise LockGateError(
                "the worktree execution lock is held by another live process "
                f"({record.get('owner')!r}); refusing to dispatch"
            )


def assert_authority_gate(ledger: Any, job_id: int, cid: str) -> None:
    """Require the broker's authority state to permit this dispatch.

    This is also the resume-revalidation gate: a run that resumes after a
    restart re-enters through here, so it runs the repair-consumption gate
    explicitly (a recorded repair is refused unless an independent verifier
    reviewed the real diff) before the dispatch gate runs it again.
    """
    try:
        broker_mod.assert_resume_clear(ledger, job_id, change_ids=[cid])
    except broker_mod.StaleMaterialError as exc:
        raise StaleMaterialGateError(str(exc)) from exc
    except broker_mod.BrokerError as exc:
        raise AuthorityGateError(str(exc)) from exc
    try:
        broker_mod.assert_dispatchable(ledger, job_id, cid)
    except broker_mod.BrokerError as exc:
        raise AuthorityGateError(str(exc)) from exc
    assert_repair_gate(ledger, job_id, cid, transition="resume")


def assert_material_freshness(ledger: Any, job_id: int, gate: Mapping[str, Any]) -> None:
    """Require the immutable job plan and policy anchors to be unchanged.

    The registration anchors are the manifest-snapshot hash and the policy
    operator revision captured when the supervised gate was opened. A policy
    revision or a re-registered snapshot changes the current policy row, so the
    next action blocks with a named stale-material error rather than running
    against material the operator changed mid-run. Unrelated repository edits
    do not affect either anchor.
    """
    policy = gate.get("policy") or {}
    anchor_revision = gate.get("policy_revision", policy.get("revision"))
    anchor_hash = gate.get("manifest_snapshot_hash", policy.get("manifest_snapshot_hash"))
    if anchor_revision is None and anchor_hash is None:
        return
    current = ledger.current_policy(job_id)
    if anchor_revision is not None and int(current["revision"]) != int(anchor_revision):
        raise StaleMaterialGateError(
            "the job policy operator revision changed since registration "
            f"({anchor_revision} -> {current['revision']}); dispatch is blocked "
            "until the operator explicitly acknowledges the new revision"
        )
    if anchor_hash is not None and str(current["manifest_snapshot_hash"]) != str(anchor_hash):
        raise StaleMaterialGateError(
            "the protected manifest snapshot hash changed since registration; "
            "dispatch is blocked against stale material"
        )
    manifest_path = gate.get("manifest_path")
    if not manifest_path:
        raise StaleMaterialGateError(
            "the current plan path is unavailable; dispatch cannot verify the "
            "protected manifest snapshot"
        )
    try:
        manifest_content = Path(manifest_path).read_text(encoding="utf-8")
    except OSError as exc:
        raise StaleMaterialGateError(
            f"the current plan cannot be read for freshness validation: {exc}"
        ) from exc
    if ledger_mod.snapshot_digest(manifest_content) != str(anchor_hash):
        raise StaleMaterialGateError(
            "the current plan on disk differs from the protected manifest "
            "snapshot recorded at registration"
        )


def assert_model_policy_gate(
    policy: Mapping[str, Any], role: str, *, resolved_model: str | None = None
) -> None:
    """Require the model policy to allow *role* against its pinned identity.

    The resolved identity is the model the dispatch will actually request. A
    missing identity, missing pin, unallowlisted model, identity mismatch, or
    legacy-unversioned policy blocks with its named reason. The policy pin is
    never substituted for absent runtime identity.
    """
    decision = model_policy_mod.check_dispatch(
        policy, role=role, resolved_model=resolved_model
    )
    if not decision.get("allowed"):
        raise ModelPolicyGateError(
            decision.get("reason") or f"model policy refused role '{role}'"
        )


def assert_egress_gate(
    ledger: Any,
    job_id: int,
    *,
    environ: Mapping[str, str] | None = None,
    config: Mapping[str, Any] | None = None,
) -> agent_contracts_mod.TransportDecision:
    """Require enforced model transport before a supervised worker is spawned.

    This is the same named, fail-closed step the session bridge runs before a
    prompt: model traffic must flow through the trusted gateway or an
    equivalently enforced isolated transport, and a worker environment must
    carry no reusable provider credential. A supervised stage-worker dispatch
    is a service-owned spawn inside the isolated worker domain, so the
    isolated-transport path holds for a credential-free environment; a leak or
    an unenforced target blocks with the named error before any spawn exists.
    """
    environment = os.environ if environ is None else environ
    options: dict[str, Any] = dict(config or {})
    options.setdefault("worker_domain_spawn", True)
    try:
        return agent_contracts_mod.assert_pre_prompt_transport(
            None,
            None,
            environ=environment,
            config=options,
            record=False,
        )
    except agent_contracts_mod.EgressEnforcementError as exc:
        raise EgressGateError(
            str(exc), decision=getattr(exc, "decision", None)
        ) from exc


# ---------------------------------------------------------------------------
# Reservation mechanics (intent -> reserve -> dispatch record)
# ---------------------------------------------------------------------------


def reserve_for_dispatch(
    repo: Path,
    cfg: dict,
    gate: Mapping[str, Any],
    cid: str,
    stage: str,
    round_num: int,
    r: dict,
    run_id: str,
    *,
    resolved_model: str | None = None,
    transport_decision: "agent_contracts_mod.TransportDecision | None" = None,
) -> dict:
    """Reserve budget for one supervised dispatch, blocking on any failure.

    Returns ``{"reservation_id", "action_id", "role", "estimate"}`` on success
    or ``{"blocked": reason, "last_result": <state>}`` when the dispatch must
    not proceed. Raises nothing: every budget failure becomes an actionable
    blocked state so the run loop can surface it.

    *transport_decision*, when supplied, is the already-evaluated pre-prompt
    egress decision: it is recorded as action evidence inside the intent window
    and **before** the dispatch record, so the journal shows enforcement before
    the side effect rather than a usage observation afterward.
    """
    ledger = gate["ledger"]
    job_id = int(gate["job_id"])
    policy = gate["policy"]
    escalation_active = bool(r.get("escalation", {}).get("active"))
    role = telemetry_mod.resolve_stage_role(stage, escalation_active=escalation_active)
    if role is None:
        return {"skipped": True}
    try:
        budgets_payload, deadlines_payload = budget_mod.enforce_policy(policy)
    except budget_mod.BudgetError as exc:
        return {
            "blocked": f"supervised budget policy blocked: {exc}",
            "last_result": "budget_policy_blocked",
        }
    try:
        elapsed_now = execution_elapsed_minutes(ledger, job_id)
        budget_mod.check_execution_deadline(
            deadlines_payload, execution_elapsed_minutes=elapsed_now
        )
    except budget_mod.BudgetError as exc:
        return {
            "blocked": (
                f"supervised execution deadline blocked: {exc}; operator action "
                "required: record an explicit policy revision extending "
                "execution_deadline_minutes"
            ),
            "last_result": "budget_deadline_exhausted",
        }
    signature = budget_mod.incident_signature(
        kind=stage, change_id=cid, stage=stage, discriminator="dispatch"
    )
    try:
        limit = budget_mod.max_incident_attempts(policy)
    except budget_mod.BudgetError as exc:
        return {
            "blocked": f"supervised budget gate blocked: {exc}",
            "last_result": "budget_policy_blocked",
        }
    if limit is not None and ledger.incident_attempt_count(job_id, signature) >= limit:
        exc = budget_mod.BoundedAttemptsExceededError(
            f"incident signature {signature} reached max_incident_attempts "
            f"({limit}); an operator revision is required to attempt it again"
        )
        return {
            "blocked": (
                f"supervised bounded attempts blocked: {exc}; operator action "
                "required: resolve the incident or raise max_incident_attempts "
                "through an explicit policy revision"
            ),
            "last_result": "bounded_attempts_exceeded",
        }
    timeout_minutes = float(cfg["changes"][cid]["timeout_minutes"])
    estimated: dict[str, float | None] = {"value": None}

    estimate_state: dict[str, object] = {"value": None, "catalog_version": None}
    dispatch_state: dict[str, int | None] = {
        "action_id": None,
        "reservation_id": None,
        "dispatch_id": None,
    }
    # Guard so a bounded-backoff retry never records the transport decision
    # twice against the same action.
    transport_state: dict[str, bool] = {"recorded": False}

    def _fail_orphan_intent(reason: str) -> None:
        action_id = dispatch_state["action_id"]
        if action_id is None or dispatch_state["dispatch_id"] is not None:
            return
        try:
            if ledger.get_action(int(action_id))["state"] == "intent":
                ledger.fail_action(int(action_id), detail=reason)
        except ledger_mod.LedgerError:
            pass

    def _attempt_reservation() -> tuple[int, int]:
        if estimate_state["value"] is None:
            estimate, catalog_version = reservation_estimate_for_dispatch(
                repo, policy, role
            )
            estimate_state["value"] = estimate
            estimate_state["catalog_version"] = catalog_version
            estimated["value"] = estimate
        if dispatch_state["action_id"] is None:
            action_detail: dict[str, Any] = {
                "change_id": cid,
                "stage": stage,
                "round": round_num,
            }
            if stage == "implement":
                action_detail["pending_automatable_tasks"] = (
                    state_mod.remaining_automatable_tasks(repo, cid)
                )
            dispatch_state["action_id"] = ledger.begin_action(
                job_id,
                kind=stage,
                run_id=run_id,
                detail=json.dumps(action_detail, sort_keys=True),
            )
        action_id = int(dispatch_state["action_id"])
        reservation_id = dispatch_state["reservation_id"]
        if reservation_id is None:
            reservation_id = budget_mod.reserve(
                ledger,
                job_id=job_id,
                action_id=action_id,
                role=role,
                requested_model=resolved_model or "",
                reserved_cost_usd=float(estimate_state["value"]),
                reserved_elapsed_minutes=timeout_minutes,
                policy=policy,
                pricing_catalog_version=estimate_state["catalog_version"],
            )
            dispatch_state["reservation_id"] = int(reservation_id)
        if transport_decision is not None and not transport_state["recorded"]:
            # Enforcement before the side effect: the decision is evidence on
            # the action, written before the dispatch record that authorizes
            # the spawn.
            ledger.record_evidence(
                action_id,
                kind=agent_contracts_mod.EVIDENCE_TRANSPORT_DECISION,
                payload=transport_decision.as_dict(),
            )
            transport_state["recorded"] = True
        if dispatch_state["dispatch_id"] is None:
            begin_active_dispatch(ledger, job_id, action_id)
            try:
                dispatch_state["dispatch_id"] = ledger.dispatch_action(action_id)
            except BaseException:
                end_active_dispatch(action_id)
                raise
        return action_id, int(reservation_id)

    retry_signature = budget_mod.incident_signature(
        kind="budget_gate_retry", change_id=cid, stage=stage,
        discriminator="ledger_write",
    )

    def _record_retry(attempt: int, delay: float, exc: BaseException) -> None:
        try:
            ledger.record_incident_attempt(job_id, signature=retry_signature)
        except Exception:
            pass
        base.log(
            f"  supervised budget gate transient failure ({exc}); retrying "
            f"({attempt}/{budget_mod.BACKOFF_MAX_ATTEMPTS}) in {delay:g}s"
        )

    def _is_transient(exc: BaseException) -> bool:
        return isinstance(exc, (sqlite3.OperationalError, RetryableCatalogLoadError))

    try:
        action_id, reservation_id = budget_mod.run_with_bounded_backoff(
            _attempt_reservation,
            on_retry=_record_retry,
            should_retry=_is_transient,
        )
    except budget_mod.BudgetExhaustedError as exc:
        _fail_orphan_intent(str(exc))
        return {
            "blocked": (
                f"supervised budget exhausted: {exc}; operator action required: "
                "record an explicit policy revision raising the limit"
            ),
            "last_result": "budget_exhausted",
        }
    except budget_mod.UnknownPricingError as exc:
        _fail_orphan_intent(str(exc))
        return {
            "blocked": (
                f"supervised unknown pricing blocked: {exc}; operator action "
                "required: qualify the role's pinned model in the pricing "
                "catalog, then register an updated policy revision"
            ),
            "last_result": "unknown_pricing",
        }
    except RetryableCatalogLoadError as exc:
        _fail_orphan_intent(str(exc))
        return {
            "blocked": (
                "supervised pricing catalog unavailable after bounded retries: "
                f"{exc}; operator action required: reinstall the matching "
                "runtime or restore the pricing catalog"
            ),
            "last_result": "catalog_unavailable",
        }
    except budget_mod.BudgetError as exc:
        _fail_orphan_intent(str(exc))
        return {
            "blocked": (
                f"supervised budget policy blocked: {exc}; operator action "
                "required: record an explicit policy revision with a versioned "
                "budget payload"
            ),
            "last_result": "budget_policy_blocked",
        }
    except Exception as exc:
        _fail_orphan_intent(str(exc))
        return {
            "blocked": (
                "supervised reservation could not be recorded after bounded "
                f"retries: {exc}"
            ),
            "last_result": "reservation_failed",
        }
    return {
        "reservation_id": reservation_id,
        "action_id": action_id,
        "role": role,
        "estimate": estimated["value"],
    }


def gated_dispatch(
    repo: Path,
    cfg: dict,
    gate: Mapping[str, Any],
    cid: str,
    stage: str,
    round_num: int,
    r: dict,
    run_id: str,
    *,
    resolved_model: str | None = None,
    environ: Mapping[str, str] | None = None,
) -> dict:
    """Run the whole boundary: gates, then intent, reserve, and dispatch record.

    Raises a named :class:`DispatchGateError` for every gate failure and for a
    refused budget reservation, before any side effect of the action. On
    success the action is journaled as dispatched and the worker identity is
    bound when the caller spawns it. The pre-prompt egress enforcement runs as
    a named gate after the model-policy gate and before the reserve/spawn, and
    its decision is recorded as action evidence before the dispatch record.
    """
    escalation_active = bool(r.get("escalation", {}).get("active"))
    role = telemetry_mod.resolve_stage_role(stage, escalation_active=escalation_active)
    transport_decision: "agent_contracts_mod.TransportDecision | None" = None
    if role is not None:
        transport_decision = evaluate_gates(
            repo, gate,
            cid=cid, stage=stage, role=role, resolved_model=resolved_model,
            environ=environ,
        )
    result = reserve_for_dispatch(
        repo, cfg, gate, cid, stage, round_num, r, run_id,
        resolved_model=resolved_model,
        transport_decision=transport_decision,
    )
    if isinstance(result, dict) and result.get("blocked"):
        raise BudgetGateError(
            result["blocked"],
            last_result=result.get("last_result", "budget_blocked"),
        )
    return result


# ---------------------------------------------------------------------------
# Outcome resolution and evidence
# ---------------------------------------------------------------------------


def journal_disposition(outcome: Any) -> str:
    """Map an engine outcome to a journal disposition.

    A confirmed dispatch completes; anything that leaves the external effect
    unconfirmed is explicitly uncertain.
    """
    value = (str(outcome) if outcome is not None else "").strip().lower()
    if value in _SUCCESSFUL_OUTCOMES:
        return "completed"
    if value in _FAILED_OUTCOMES:
        return "failed"
    return "uncertain"


def resolve_dispatch(
    gate: Mapping[str, Any],
    *,
    action_id: int | None = None,
    reservation_id: int | None = None,
    outcome: Any,
    record: Any = None,
) -> str | None:
    """Resolve a dispatched action to a terminal or uncertain state.

    The reservation is reconciled from the observed usage exactly as before;
    the action then records outcome/usage evidence and either completes, fails,
    or — when the outcome cannot be confirmed — is marked uncertain so a run
    cannot silently progress past it.
    """
    ledger = gate["ledger"]
    try:
        if reservation_id is not None:
            reconcile_reservation(ledger, int(reservation_id), record, outcome)
        if action_id is None:
            return None
        action_id = int(action_id)
        try:
            state = ledger.get_action(action_id)["state"]
        except ledger_mod.UnknownRecordError:
            return None
        if state in JOURNAL_TERMINAL_STATES:
            return state
        disposition = journal_disposition(outcome)
        if state == "uncertain":
            return "uncertain"
        if disposition in JOURNAL_TERMINAL_STATES and str(outcome) not in (
            "spawn_error", "env_error",
        ):
            dispatch = ledger.latest_dispatch(action_id)
            process_identity = (
                parse_process_identity(dispatch["process_id"])
                if dispatch is not None else None
            )
            session_identity = (
                str(dispatch["session_id"]).strip()
                if dispatch is not None and dispatch["session_id"] else ""
            )
            if process_identity is None and not session_identity:
                disposition = "uncertain"
        if disposition == "completed":
            _record_terminal_evidence(ledger, action_id, outcome, record, completed=True)
            ledger.complete_action(action_id)
        elif disposition == "failed":
            _record_terminal_evidence(ledger, action_id, outcome, record, completed=False)
            ledger.fail_action(action_id, detail=str(outcome))
        else:
            _record_uncertain_evidence(ledger, action_id, outcome, record)
            if state != "uncertain":
                detail: dict[str, Any] = {"uncertainty": str(outcome)}
                try:
                    existing = json.loads(ledger.get_action(action_id)["detail"] or "{}")
                    if isinstance(existing, dict):
                        detail = {**existing, **detail}
                except (TypeError, ValueError):
                    pass
                ledger.mark_uncertain(
                    action_id, detail=json.dumps(detail, sort_keys=True)
                )
        return disposition
    finally:
        end_active_dispatch(action_id)


def reconcile_reservation(
    ledger: Any, reservation_id: int, record: Any, outcome: Any
) -> None:
    """Reconcile observed usage against a reservation without double billing."""
    try:
        usage = record.get("usage", {}) if isinstance(record, dict) else {}
        cost = record.get("cost", {}) if isinstance(record, dict) else {}
        usage_available = bool(usage.get("usage_available"))
        cost_status = cost.get("status")
        if outcome in ("timeout", "spawn_error", "env_error"):
            observation_state = "interrupted"
        elif usage_available and cost_status == "estimated":
            observation_state = "observed"
        else:
            observation_state = "unknown"
        if observation_state == "observed":
            budget_mod.reconcile(
                ledger,
                reservation_id=reservation_id,
                observation_state="observed",
                observed_input_tokens=usage.get("input_tokens"),
                observed_output_tokens=usage.get("output_tokens"),
                observed_cached_tokens=usage.get("cached_input_tokens"),
                observed_reasoning_tokens=usage.get("reasoning_tokens"),
                observed_cost_usd=cost.get("estimated_cost"),
                observed_elapsed_minutes=(
                    float(record.get("duration_ms", 0)) / 60000.0
                    if isinstance(record, dict) else None
                ),
            )
        else:
            budget_mod.reconcile(
                ledger, reservation_id=reservation_id,
                observation_state=observation_state,
            )
    except budget_mod.BudgetError:
        pass
    except Exception:
        pass


def _record_terminal_evidence(
    ledger: Any, action_id: int, outcome: Any, record: Any, *, completed: bool
) -> None:
    payload: dict[str, Any] = {
        "outcome": str(outcome), "confirmed": True, "completed": completed,
    }
    usage = record.get("usage") if isinstance(record, dict) else None
    if usage:
        payload["usage"] = usage
    ledger.record_evidence(action_id, kind=EVIDENCE_STAGE_RESULT, payload=payload)
    if usage:
        ledger.record_evidence(action_id, kind=EVIDENCE_USAGE, payload=usage)


def _record_uncertain_evidence(
    ledger: Any, action_id: int, outcome: Any, record: Any
) -> None:
    value = str(outcome) if outcome is not None else ""
    kind = (
        EVIDENCE_SPAWN_LOSS
        if value in ("spawn_error", "env_error")
        else EVIDENCE_STAGE_RESULT
    )
    payload: dict[str, Any] = {"outcome": value, "confirmed": False}
    usage = record.get("usage") if isinstance(record, dict) else None
    if usage:
        payload["usage"] = usage
    ledger.record_evidence(action_id, kind=kind, payload=payload)


def record_session_binding(ledger: Any, action_id: int, session_id: str) -> int:
    """Bind a worker-reported native Task session identity to the action.

    The session identity is written onto the action's dispatch row and
    journaled as a ``session_binding`` evidence entry, so the native Task path
    is tracked in the same journal as subprocess dispatches.
    """
    ledger.bind_dispatch_identity(int(action_id), session_id=str(session_id))
    return ledger.record_evidence(
        int(action_id),
        kind=EVIDENCE_SESSION_BINDING,
        payload={"session_id": str(session_id)},
    )


# ---------------------------------------------------------------------------
# Pending-uncertainty inventory and replay
# ---------------------------------------------------------------------------


def reconcile_pending(ledger: Any, job_id: int | None = None) -> list[dict[str, Any]]:
    """Inventory a job's unconfirmed actions as blocking state.

    A run that resumes with any entry here must not dispatch the next action as
    though the uncertain one had succeeded or never happened; recorded evidence
    reconciles the action first. A stale ``dispatched`` row is first made
    explicitly ``uncertain`` because its controller stopped before recording an
    outcome. Accepts either a ``(ledger, job_id)`` pair or a gate mapping
    carrying both.
    """
    if isinstance(ledger, Mapping):
        job_id = int(ledger["job_id"])
        ledger = ledger["ledger"]
    if job_id is None:
        raise TypeError("reconcile_pending requires a job id")
    for row in ledger.list_actions(int(job_id)):
        if row["state"] != "dispatched":
            continue
        detail: dict[str, Any] = {
            "uncertainty": "controller stopped before recording an outcome"
        }
        try:
            decoded = json.loads(row["detail"] or "{}")
            if isinstance(decoded, dict):
                detail = {**decoded, **detail}
        except (TypeError, ValueError):
            pass
        ledger.mark_uncertain(
            int(row["id"]), detail=json.dumps(detail, sort_keys=True)
        )
    pending: list[dict[str, Any]] = []
    for row in ledger.list_uncertain_actions(int(job_id)):
        reservation = ledger.reservation_for_action(int(row["id"]))
        if reservation is not None and reservation["state"] == "reserved":
            ledger.retain_reservation(int(reservation["id"]))
        detail: dict[str, Any] = {}
        try:
            decoded = json.loads(row["detail"] or "{}")
            if isinstance(decoded, dict):
                detail = decoded
        except (TypeError, ValueError):
            pass
        pending.append(
            {
                "action_id": int(row["id"]),
                "kind": row["kind"],
                "run_id": row["run_id"],
                "detail": row["detail"],
                "change_id": detail.get("change_id"),
                "stage": detail.get("stage"),
            }
        )
    return pending


def _decisive_evidence(ledger: Any, action_id: int) -> dict[str, Any] | None:
    """Return a decisive delivered-result evidence row, or ``None``.

    Only a confirmed ``stage_result`` (or a ``spawn_loss``) is decisive for
    reconciliation; usage and unknown kinds are billing/observation evidence
    and never resolve an action on their own.
    """
    for row in ledger.list_evidence(action_id):
        if row["kind"] not in (EVIDENCE_STAGE_RESULT, EVIDENCE_SPAWN_LOSS):
            continue
        payload = row["payload"]
        if not isinstance(payload, str):
            continue
        try:
            decoded = json.loads(payload)
        except (TypeError, ValueError):
            continue
        if not isinstance(decoded, dict) or not decoded.get("confirmed"):
            continue
        if row["kind"] == EVIDENCE_SPAWN_LOSS:
            return {"kind": row["kind"], "payload": decoded, "completed": False}
        completed = decoded.get("completed")
        outcome = str(decoded.get("outcome", "")).strip().lower()
        if completed is not True and completed is not False:
            if outcome in _SUCCESSFUL_OUTCOMES:
                completed = True
            elif outcome in _FAILED_OUTCOMES:
                completed = False
            else:
                continue
        return {
            "kind": row["kind"],
            "payload": decoded,
            "completed": completed,
        }
    return None


def _prior_process_state(ledger: Any, action_id: int) -> str:
    row = ledger.latest_dispatch(action_id)
    if row is None:
        return "unfenceable"
    if not row["process_id"]:
        return "unfenceable"
    identity = parse_process_identity(row["process_id"])
    if not identity:
        return "unfenceable"
    try:
        pid = int(identity.get("pid"))
    except (TypeError, ValueError):
        return "unfenceable"
    if pid <= 0 or identity.get("boot_id") != lock_mod.boot_identity():
        return "dead"
    observed = lock_mod.process_start_time(pid)
    if observed is None:
        return "dead"
    try:
        return (
            "live"
            if float(observed) == float(identity.get("process_start"))
            else "dead"
        )
    except (TypeError, ValueError):
        return "unfenceable"


def replay_uncertain(
    repo: Path,
    cfg: dict,
    gate: Mapping[str, Any],
    action_id: int,
    *,
    cid: str | None = None,
    round_num: int | None = None,
    r: dict | None = None,
    run_id: str | None = None,
    resolved_model: str | None = None,
    reobserve: Any = None,
) -> dict[str, Any]:
    """Replay an uncertain action only after deduplication and re-observation.

    A delivered result already on the action reconciles it instead of
    replaying; otherwise the caller's ``reobserve`` callback inspects the
    external state the prior attempt may have affected. Replay happens only
    when that observation shows the work incomplete, and a live prior worker
    (matched on the recorded process identity, not a bare PID) fences replay.
    """
    ledger = gate["ledger"]
    try:
        state = ledger.get_action(action_id)["state"]
    except ledger_mod.UnknownRecordError:
        return {"replayed": False, "reason": "unknown_action"}
    if state in JOURNAL_TERMINAL_STATES:
        return {"replayed": False, "reason": "terminal", "state": state}

    decisive = _decisive_evidence(ledger, action_id)
    if decisive is not None:
        terminal_state = _apply_decisive(ledger, action_id, decisive)
        return {
            "replayed": False,
            "reason": "reconciled",
            "state": terminal_state,
        }

    if state != "uncertain":
        return {"replayed": False, "reason": "not_uncertain", "state": state}

    observed = reobserve() if reobserve is not None else None
    if observed is True:
        ledger.record_evidence(
            action_id,
            kind=EVIDENCE_STAGE_RESULT,
            payload={"confirmed": True, "observed": "complete"},
        )
        ledger.reconcile_action(action_id)
        ledger.complete_action(action_id)
        return {"replayed": False, "reason": "observed_complete"}
    if observed is None:
        return {"replayed": False, "reason": "observation_unknown"}
    prior_process = _prior_process_state(ledger, action_id)
    if prior_process == "live":
        return {"replayed": False, "reason": "prior_worker_live"}
    if prior_process == "unfenceable":
        return {"replayed": False, "reason": "prior_identity_unfenceable"}

    action = ledger.get_action(action_id)
    detail: dict[str, Any] = {}
    try:
        decoded_detail = json.loads(action["detail"] or "{}")
        if isinstance(decoded_detail, dict):
            detail = decoded_detail
    except (TypeError, ValueError):
        pass
    replay_cid = cid or detail.get("change_id")
    replay_stage = str(detail.get("stage") or action["kind"])
    replay_round = int(round_num or detail.get("round") or 1)
    replay_run_id = run_id or str(action["run_id"])
    if not replay_cid:
        return {"replayed": False, "reason": "replay_context_missing"}

    ledger.record_evidence(
        action_id,
        kind=EVIDENCE_STAGE_RESULT,
        payload={"confirmed": False, "observed": "incomplete"},
    )
    ledger.reconcile_action(action_id)
    ledger.fail_action(action_id, detail="superseded by a fenced replay")
    replacement = gated_dispatch(
        repo,
        cfg,
        gate,
        str(replay_cid),
        replay_stage,
        replay_round,
        r or {"escalation": {"active": False}},
        replay_run_id,
        resolved_model=resolved_model,
    )
    return {
        "replayed": True,
        "prior_action_id": action_id,
        "action_id": replacement.get("action_id"),
        "reservation_id": replacement.get("reservation_id"),
        "role": replacement.get("role"),
    }


def _apply_decisive(ledger: Any, action_id: int, decisive: Mapping[str, Any]) -> str:
    state = ledger.get_action(action_id)["state"]
    if state == "uncertain":
        ledger.reconcile_action(action_id)
    if decisive.get("completed"):
        ledger.complete_action(action_id)
        return "completed"
    ledger.fail_action(action_id, detail="reconciled failure")
    return "failed"


__all__ = [
    "AuthorityGateError",
    "BudgetGateError",
    "DispatchGateError",
    "EVIDENCE_KINDS",
    "EVIDENCE_SESSION_BINDING",
    "EVIDENCE_SPAWN_LOSS",
    "EVIDENCE_STAGE_RESULT",
    "EVIDENCE_USAGE",
    "GATE_AUTHORITY",
    "GATE_BUDGET",
    "GATE_LOCK",
    "GATE_MODEL_POLICY",
    "GATE_STALE_MATERIAL",
    "JournalDispatchError",
    "LockGateError",
    "ModelPolicyGateError",
    "RepairGateError",
    "RetryableCatalogLoadError",
    "StaleMaterialGateError",
    "active_dispatch",
    "assert_authority_gate",
    "assert_egress_gate",
    "assert_lock_gate",
    "assert_material_freshness",
    "assert_model_policy_gate",
    "assert_repair_gate",
    "begin_active_dispatch",
    "end_active_dispatch",
    "evaluate_gates",
    "execution_elapsed_minutes",
    "gated_dispatch",
    "journal_disposition",
    "mark_active_uncertain",
    "note_spawned_process",
    "parse_process_identity",
    "pinned_model_for_role",
    "reconcile_pending",
    "reconcile_reservation",
    "record_session_binding",
    "replay_uncertain",
    "reservation_estimate_for_dispatch",
    "resolve_dispatch",
    "reserve_for_dispatch",
    "serialize_process_identity",
]
