"""Operator/worker endpoint split with kernel-checked peer credentials.

The supervision service exposes two Unix-domain sockets with disjoint verb
surfaces:

- the **operator endpoint**, which accepts only connections whose kernel-reported
  peer uid is the configured operator principal, and
- the **worker-actions endpoint**, which serves only the scoped job-service
  verbs a worker may request and carries no operator authority.

Authentication is the kernel-reported peer uid (``SO_PEERCRED``). There is no
bearer token, capability file, or environment variable that could leak into a
worker domain, and this module models that absence explicitly rather than
leaving it implied.

Design rules enforced here:

- Standard library only, no import of another runtime package.
- The endpoint handler tables are disjoint: no handler is reachable from both
  endpoints, so an operator-only verb cannot be smuggled through the worker
  endpoint by a flag.
- A mismatched or unverifiable peer is closed before any request is read, so no
  unauthenticated byte is ever interpreted.
- No verb or handler reaches a process-execution primitive, so
  repository-controlled code never executes as the service identity; see
  :func:`dispatcher_executes_repo_code`.
"""

from __future__ import annotations

import json
import socket
import struct
from dataclasses import dataclass, field
from types import MappingProxyType
from typing import Any, Callable, Iterable, Mapping

from lib.supervisor import agent_contracts as agent_contracts_module
from lib.supervisor import broker as broker_module
from lib.supervisor import lifecycle as lifecycle_module

ENDPOINT_OPERATOR = "operator"
ENDPOINT_WORKER = "worker-actions"

# This surface never carries operator credential material. Kept as an explicit
# constant so tests can assert the property rather than infer it from silence.
USES_TOKEN_MATERIAL = False

# Worker evidence kinds that may be decisive for reconciliation, plus the
# explicit terminal outcomes that classify a confirmed result. Everything else
# (usage, session binding, unknown kinds, unconfirmed payloads, or a confirmed
# payload with no explicit terminal result) is non-decisive and leaves an
# action's state untouched.
_DECISIVE_EVIDENCE_KINDS = frozenset({"stage_result", "spawn_loss"})
_TERMINAL_SUCCESS_OUTCOMES = frozenset({"completed", "exited", "done"})


class EndpointError(Exception):
    """An endpoint dispatch requested a verb it does not expose."""


class PeerCredentialError(Exception):
    """A peer's credentials could not be read or did not match the endpoint."""


@dataclass(frozen=True)
class PeerCredentials:
    """The kernel-reported identity of a connected peer."""

    pid: int
    uid: int
    gid: int


# ---------------------------------------------------------------------------
# Accept plumbing
# ---------------------------------------------------------------------------


def _close_quietly(conn: Any) -> None:
    try:
        conn.close()
    except OSError:  # pragma: no cover - best-effort close
        pass


def peer_credentials(conn: Any) -> PeerCredentials:
    """Read a connected socket's peer credentials via ``SO_PEERCRED``.

    Raises :class:`PeerCredentialError` when the platform does not support peer
    credentials or the lookup fails. A lookup failure is never treated as an
    authenticated peer.
    """
    option = getattr(socket, "SO_PEERCRED", None)
    if option is None:
        raise PeerCredentialError(
            "platform does not expose SO_PEERCRED; peer authentication is unavailable"
        )
    try:
        raw = conn.getsockopt(socket.SOL_SOCKET, option, struct.calcsize("3i"))
    except (OSError, AttributeError) as exc:
        raise PeerCredentialError(f"peer credential lookup failed: {exc}") from exc
    try:
        pid, uid, gid = struct.unpack("3i", raw)
    except struct.error as exc:  # pragma: no cover - malformed kernel payload
        raise PeerCredentialError(f"peer credential payload was malformed: {exc}") from exc
    return PeerCredentials(pid=pid, uid=uid, gid=gid)


def accept_verified_peer(conn: Any, *, allowed_uids: Iterable[int]) -> PeerCredentials:
    """Verify *conn*'s peer uid against *allowed_uids*, closing it on failure.

    Every unverifiable peer is closed before the caller may read anything from
    it: a credential-lookup failure and a uid mismatch both close the
    connection. The returned credentials identify the authenticated peer.
    """
    allowed = frozenset(allowed_uids)
    try:
        credentials = peer_credentials(conn)
    except PeerCredentialError:
        _close_quietly(conn)
        raise
    if credentials.uid not in allowed:
        _close_quietly(conn)
        raise PeerCredentialError(
            f"peer uid {credentials.uid} is not an allowed principal for this "
            f"endpoint (allowed: {sorted(allowed)})"
        )
    return credentials


# ---------------------------------------------------------------------------
# Disjoint dispatch tables
# ---------------------------------------------------------------------------


def _principal_for(credentials: PeerCredentials, role: str, name: str | None = None) -> "broker_module.BrokerPrincipal":
    return broker_module.BrokerPrincipal(role=role, name=name or "", uid=credentials.uid)


def _broker_ledger(request: Mapping[str, Any]) -> tuple[Any, int]:
    """Return the ledger handle and job id a broker handler operates on.

    The transport supplies the service-owned ledger on the request; a missing
    handle means the broker path cannot be reached, so the handler fails closed
    with a named error rather than validating without recording.
    """
    ledger = request.get("ledger")
    job_id = request.get("job_id")
    if ledger is None or job_id is None:
        raise broker_module.BrokerUnavailableError(
            "the broker path is unavailable: no service-owned ledger was "
            "supplied to the endpoint"
        )
    return ledger, int(job_id)


def _requested_change_ids(request: Mapping[str, Any]) -> list[str]:
    change_ids = request.get("change_ids")
    if change_ids is None:
        single = request.get("change_id")
        change_ids = [single] if single else []
    if isinstance(change_ids, (str, bytes)) or not isinstance(change_ids, Iterable):
        raise broker_module.BrokerError("change_ids must be an iterable of ids")
    return [str(cid) for cid in change_ids]


def _require_mutable_job(ledger: Any, job_id: int) -> Any:
    """Return the job row, refusing a terminal job with the named error.

    Every mutating operator request re-checks the durable job state, so a
    cancelled (or otherwise terminal) job refuses all further receipts, stop
    requests, and lifecycle verbs with the named terminal-job error and records
    nothing.
    """
    job = lifecycle_module.require_job(ledger, job_id)
    if str(job["state"]) in lifecycle_module.TERMINAL_JOB_STATES:
        raise lifecycle_module.TerminalJobError(
            f"job {job_id} is {job['state']}; a terminal job refuses all "
            "further receipts, stop requests, and lifecycle verbs"
        )
    return job


def _operator_approve(request: Mapping[str, Any], credentials: PeerCredentials) -> dict[str, Any]:
    ledger, job_id = _broker_ledger(request)
    _require_mutable_job(ledger, job_id)
    principal = _principal_for(credentials, broker_module.OPERATOR)
    recorded = broker_module.record_approval(
        ledger, job_id, principal=principal,
        change_ids=_requested_change_ids(request),
    )
    return {
        "verb": "approve",
        "operator_uid": credentials.uid,
        "approved": [receipt.change_id for receipt in recorded],
        "receipts": [receipt.as_dict() for receipt in recorded],
    }


def _operator_reset_change(
    request: Mapping[str, Any], credentials: PeerCredentials
) -> dict[str, Any]:
    ledger, job_id = _broker_ledger(request)
    _require_mutable_job(ledger, job_id)
    principal = _principal_for(credentials, broker_module.OPERATOR)
    change_ids = _requested_change_ids(request)
    # A reset is a repair-consuming transition: an authorized reset that would
    # discard a supervised repair is refused until an independent verifier
    # verdict consumes it. A change with no recorded repair passes unchanged.
    for change_id in change_ids:
        _gate_repair_consumption(
            ledger,
            job_id,
            change_id=change_id,
            transition="reset",
            request=request,
        )
    recorded = [
        broker_module.reset_change(
            ledger, job_id, principal=principal, change_id=change_id
        )
        for change_id in change_ids
    ]
    return {
        "verb": "reset_change",
        "operator_uid": credentials.uid,
        "reset": [receipt.change_id for receipt in recorded],
        "receipts": [receipt.as_dict() for receipt in recorded],
    }


def _gate_repair_consumption(
    ledger: Any,
    job_id: int,
    *,
    change_id: str,
    transition: str,
    request: Mapping[str, Any],
) -> dict[str, Any]:
    """Run the repair-consumption gate for one change, naming the transition."""
    return agent_contracts_module.assert_repair_consumable(
        ledger,
        int(job_id),
        transition=transition,
        change_id=str(change_id),
        fixer_report=request.get("fixer_report"),
        verifier_verdict=request.get("verifier_verdict"),
        fixer_session_id=request.get("fixer_session_id"),
        verifier_session_id=request.get("verifier_session_id"),
        action_id=request.get("action_id"),
    )


def _worker_release_delegated_gate(
    request: Mapping[str, Any], credentials: PeerCredentials
) -> dict[str, Any]:
    ledger, job_id = _broker_ledger(request)
    _require_worker_contract(request, ledger, job_id)
    _require_mutable_job(ledger, job_id)
    identity = request.get("service_identity")
    principal = _principal_for(credentials, broker_module.SERVICE, identity)
    change_id = request.get("change_id")
    if not change_id:
        raise broker_module.BrokerError("release_delegated_gate requires a change_id")
    agent_contracts_module.assert_repair_consumable(
        ledger,
        int(job_id),
        transition="release_delegated_gate",
        change_id=str(change_id),
        fixer_report=request.get("fixer_report"),
        verifier_verdict=request.get("verifier_verdict"),
        fixer_session_id=request.get("fixer_session_id"),
        verifier_session_id=request.get("verifier_session_id"),
        action_id=request.get("action_id"),
    )
    receipt = broker_module.release_delegated_gate(
        ledger, job_id, principal=principal, change_id=str(change_id)
    )
    return {
        "verb": "release_delegated_gate",
        "worker_uid": credentials.uid,
        "released": receipt.change_id,
        "receipt": receipt.as_dict(),
    }


def _operator_revise_policy(request: Mapping[str, Any], credentials: PeerCredentials) -> dict[str, Any]:
    return {
        "verb": "revise_policy",
        "operator_uid": credentials.uid,
        "revision": request.get("revision"),
    }


def _operator_enable(request: Mapping[str, Any], credentials: PeerCredentials) -> dict[str, Any]:
    return {"verb": "enable", "operator_uid": credentials.uid}


def _lifecycle_result(verb: str, job_id: int, credentials: PeerCredentials, job: Any) -> dict[str, Any]:
    return {
        "verb": verb,
        "operator_uid": credentials.uid,
        "job_id": int(job_id),
        "state": str(job["state"]),
    }


def _operator_pause(request: Mapping[str, Any], credentials: PeerCredentials) -> dict[str, Any]:
    ledger, job_id = _broker_ledger(request)
    job = lifecycle_module.pause(
        ledger,
        job_id,
        authority=broker_module.OPERATOR,
        actor_principal=str(credentials.uid),
        detail=request.get("detail"),
    )
    return _lifecycle_result("pause", job_id, credentials, job)


def _operator_drain(request: Mapping[str, Any], credentials: PeerCredentials) -> dict[str, Any]:
    ledger, job_id = _broker_ledger(request)
    job = lifecycle_module.drain(
        ledger,
        job_id,
        authority=broker_module.OPERATOR,
        actor_principal=str(credentials.uid),
        detail=request.get("detail"),
    )
    return _lifecycle_result("drain", job_id, credentials, job)


def _operator_resume(request: Mapping[str, Any], credentials: PeerCredentials) -> dict[str, Any]:
    ledger, job_id = _broker_ledger(request)
    job = lifecycle_module.resume(ledger, job_id)
    return _lifecycle_result("resume", job_id, credentials, job)


def _operator_cancel(request: Mapping[str, Any], credentials: PeerCredentials) -> dict[str, Any]:
    ledger, job_id = _broker_ledger(request)
    job = lifecycle_module.cancel(
        ledger,
        job_id,
        authority=broker_module.OPERATOR,
        actor_principal=str(credentials.uid),
        detail=request.get("detail"),
    )
    return _lifecycle_result("cancel", job_id, credentials, job)


def _worker_report_status(
    request: Mapping[str, Any], credentials: PeerCredentials
) -> dict[str, Any]:
    ledger, job_id = _broker_ledger(request)
    _require_worker_contract(request, ledger, job_id)
    return {"verb": "report_status", "worker_uid": credentials.uid, "status": request.get("status")}


def _bound_job_action(ledger: Any, job_id: int, action_id: Any) -> Any:
    """Return the action *action_id* when it belongs to the bound *job_id*.

    Evidence and action requests are accepted only for actions of the job the
    worker is bound to; a cross-job reference is refused with the named
    authorization error rather than recording against another job's action.
    """
    try:
        numeric = int(action_id)
    except (TypeError, ValueError):
        raise broker_module.BrokerError("an integer action_id is required")
    try:
        action = ledger.get_action(numeric)
    except Exception as exc:  # noqa: BLE001 - surfaced as a named broker error
        raise broker_module.BrokerError(f"unknown action {numeric}: {exc}") from exc
    if int(action["job_id"]) != int(job_id):
        raise broker_module.BrokerMediationError(
            f"action {numeric} is owned by job {action['job_id']}, not the bound "
            f"job {job_id}; refusing the worker-domain write"
        )
    return action


def _require_worker_contract(
    request: Mapping[str, Any], ledger: Any, job_id: int
) -> dict[str, Any]:
    """Refuse a supervised worker request whose identity or contract drifts.

    Every worker-domain request is a *supervised* request: it must carry its
    role, its observed concrete agent, and the job's registered service
    identity. A missing field, a spoofed identity, a mismatched agent, an
    unpinned role, a model override, and a requested capability outside the
    role allowlist are each *never* defaulted: the violation is recorded as a
    durable ``policy_violation`` incident against the job and the request is
    refused with :class:`broker_module.BrokerMediationError`. There is no
    unauthenticated legacy worker path on this endpoint.
    """
    job = ledger.get_job(int(job_id))
    identity_check = agent_contracts_module.check_worker_identity(request, job)
    if not identity_check["allowed"]:
        incident_id = agent_contracts_module.record_policy_violation(
            ledger,
            int(job_id),
            role=request.get("role"),
            observed_agent=request.get("observed_agent"),
            reason=str(identity_check["reason"]),
            detail={
                "violations": [v["kind"] for v in identity_check["violations"]]
            },
        )
        raise broker_module.BrokerMediationError(
            f"worker request refused: {identity_check['reason']} "
            f"(policy_violation incident {incident_id} recorded against job {job_id})"
        )

    role = request.get("role")
    try:
        policy = ledger.current_policy(int(job_id))
    except Exception:  # noqa: BLE001 - absent policy is durable-state absence
        policy = None
    observed = request.get("observed_agent")
    requested = request.get("requested_permissions")
    if isinstance(requested, (str, bytes)) or (
        requested is not None and not isinstance(requested, Iterable)
    ):
        raise broker_module.BrokerError(
            "requested_permissions must be an iterable of capability names"
        )
    requested_model = request.get("requested_model")
    check = agent_contracts_module.check_session_contract(
        policy,
        role,
        observed,
        list(requested) if requested is not None else None,
        requested_model=requested_model,
    )
    if check["allowed"]:
        return check
    incident_id = agent_contracts_module.record_policy_violation(
        ledger,
        int(job_id),
        role=check.get("role") or role,
        observed_agent=check.get("observed_agent"),
        expected_agent=check.get("expected_agent"),
        reason=str(check.get("reason") or "session contract violated"),
        detail={"violations": [v["kind"] for v in check["violations"]]},
    )
    raise broker_module.BrokerMediationError(
        f"worker request refused: {check['reason']} "
        f"(policy_violation incident {incident_id} recorded against job {job_id})"
    )


def _worker_report_violation(
    request: Mapping[str, Any], credentials: PeerCredentials
) -> dict[str, Any]:
    """Record a worker-detected policy violation durably against the job.

    This is how an executable-level shell-bypass attempt is surfaced rather
    than merely printed: the worker's tracked shell wrapper reports the
    attempted command, and the durable ``policy_violation`` incident is
    recorded in the same incident surface a spoof or escalation uses. The
    request is held to the worker identity contract first, so a violation
    report cannot be forged from outside the job.
    """
    ledger, job_id = _broker_ledger(request)
    _require_worker_contract(request, ledger, job_id)
    detail = request.get("detail", request.get("command"))
    if isinstance(detail, (Mapping, list, tuple)):
        detail = json.dumps(detail, sort_keys=True, default=str)
    incident_id = agent_contracts_module.record_policy_violation(
        ledger,
        int(job_id),
        role=request.get("role"),
        observed_agent=request.get("observed_agent"),
        reason=str(request.get("reason") or "worker-reported policy violation"),
        detail={"reported_command": str(detail) if detail else ""},
    )
    return {
        "verb": "report_violation",
        "worker_uid": credentials.uid,
        "incident_id": incident_id,
    }


def _worker_request_action(
    request: Mapping[str, Any], credentials: PeerCredentials
) -> dict[str, Any]:
    """Answer from journaled job state: the bound job's actionable items.

    The response lists unreconciled uncertain actions (which block silent
    progress until evidence reconciles them), steering receipts, and delegated
    gate releases. The richer lifecycle payload shape is deferred to
    ``add-supervised-plan-lifecycle``; this is the minimal actionable-items
    answer.
    """
    ledger, job_id = _broker_ledger(request)
    _require_worker_contract(request, ledger, job_id)
    uncertain = [
        {
            "action_id": int(row["id"]),
            "kind": row["kind"],
            "run_id": row["run_id"],
            "state": row["state"],
        }
        for row in ledger.list_uncertain_actions(job_id)
    ]
    try:
        high_water = int(request.get("high_water", 0))
    except (TypeError, ValueError):
        high_water = 0
    steering: list[dict[str, Any]] = []
    delegated_releases: list[dict[str, Any]] = []
    for row in ledger.receipts_after(job_id, high_water):
        item = {
            "receipt_id": int(row["id"]),
            "change_id": row["change_id"],
            "kind": row["kind"],
            "authority": row["authority"],
        }
        if row["kind"] == "steer":
            steering.append(item)
        if row["authority"] == "delegated":
            delegated_releases.append(item)
    return {
        "verb": "request_action",
        "worker_uid": credentials.uid,
        "job_id": int(job_id),
        "uncertain_actions": uncertain,
        "steering_receipts": steering,
        "delegated_releases": delegated_releases,
    }


def _evidence_payload_mapping(payload: Any) -> Mapping[str, Any]:
    """Return *payload* as a mapping, decoding a JSON object string if needed."""
    if isinstance(payload, Mapping):
        return payload
    if isinstance(payload, str) and payload:
        try:
            decoded = json.loads(payload)
        except (TypeError, ValueError):
            return {}
        if isinstance(decoded, Mapping):
            return decoded
    return {}


def _classify_worker_evidence(kind: str, payload: Any) -> str | None:
    """Classify decisive worker evidence as ``completed``/``failed`` or ``None``.

    Only a confirmed ``stage_result`` or ``spawn_loss`` with an explicit
    terminal result is decisive. A success is an ``outcome`` of
    ``completed``/``exited``/``done`` or ``completed: true``; a failure is an
    ``outcome`` of ``failed``, a ``spawn_loss``, or ``completed: false``.
    Unconfirmed payloads, other kinds, and confirmed payloads with no explicit
    terminal result are non-decisive and leave the action's state untouched.
    """
    if kind not in _DECISIVE_EVIDENCE_KINDS:
        return None
    decoded = _evidence_payload_mapping(payload)
    if not decoded.get("confirmed"):
        return None
    outcome = str(decoded.get("outcome", "") or "").strip().lower()
    completed = decoded.get("completed")
    if outcome in _TERMINAL_SUCCESS_OUTCOMES or completed is True:
        return "completed"
    if outcome == "failed" or kind == "spawn_loss" or completed is False:
        return "failed"
    return None


def _worker_record_evidence(
    request: Mapping[str, Any], credentials: PeerCredentials
) -> dict[str, Any]:
    """Persist worker-reported evidence against one of the bound job's actions.

    The caller is validated inside the worker domain (kernel-checked peer uid
    at accept, plus the bound job's registered identity when asserted) and the
    referenced action must belong to the bound job. A reported native Task
    ``session_id`` is bound to the action's dispatch row before any evidence is
    accepted, and a binding failure is surfaced rather than swallowed. Evidence
    never reconciles on its own: only decisive evidence (a confirmed
    ``stage_result``/``spawn_loss`` with an explicit terminal result) against an
    ``uncertain`` action drives the explicit reconcile-then-terminal lifecycle,
    so the returned state is ``completed``/``failed`` for a decisive report and
    unchanged otherwise.
    """
    ledger, job_id = _broker_ledger(request)
    _require_worker_contract(request, ledger, job_id)
    action_id = request.get("action_id")
    if action_id is None:
        raise broker_module.BrokerError("record_evidence requires an action_id")
    action = _bound_job_action(ledger, job_id, action_id)
    evidence = request.get("evidence")
    kind = request.get("kind")
    payload = request.get("payload")
    if isinstance(evidence, Mapping):
        if kind is None:
            kind = evidence.get("kind")
        if payload is None:
            payload = evidence.get("payload", evidence)
    if not isinstance(kind, str) or not kind:
        kind = "stage_result"

    session_id = request.get("session_id")
    bound_session: str | None = None
    if session_id is not None and str(session_id):
        bound_session = str(session_id)
        # Bind before evidence is accepted; a binding failure (for example a
        # missing dispatch row) is surfaced so the worker learns its report was
        # not journaled.
        ledger.bind_dispatch_identity(int(action_id), session_id=bound_session)
        ledger.record_evidence(
            int(action_id),
            kind="session_binding",
            payload={"session_id": bound_session},
        )

    # Worker-initiated subprocess delegation: a reported process identity is
    # bound to the owning action's dispatch row in the same journal the
    # orchestrator uses, so the delegation is tracked with the same lifecycle.
    process_identity = request.get("process_identity", request.get("process_id"))
    bound_process: str | None = None
    if process_identity is not None and process_identity != "":
        if isinstance(process_identity, Mapping):
            bound_process = json.dumps(dict(process_identity), sort_keys=True)
        elif isinstance(process_identity, str):
            bound_process = process_identity
        else:
            raise broker_module.BrokerError(
                "process_identity must be a serialized identity string or object"
            )
        ledger.bind_dispatch_identity(int(action_id), process_id=bound_process)
        ledger.record_evidence(
            int(action_id),
            kind="session_binding",
            payload={"process_identity": bound_process},
        )

    prior_state = str(action["state"])
    evidence_id = ledger.record_evidence(
        int(action_id), kind=str(kind), payload=payload
    )
    decision = _classify_worker_evidence(str(kind), payload)
    if decision is not None and prior_state == "uncertain":
        # Decisive evidence is the recorded basis for reconciliation; the
        # transition is explicit and precedes the terminal state.
        ledger.reconcile_action(int(action_id))
        if decision == "completed":
            ledger.complete_action(int(action_id))
        else:
            ledger.fail_action(int(action_id), detail="reconciled worker failure")
    try:
        state = str(ledger.get_action(int(action_id))["state"])
    except Exception:
        state = prior_state
    return {
        "verb": "record_evidence",
        "worker_uid": credentials.uid,
        "action_id": int(action_id),
        "evidence_id": int(evidence_id),
        "kind": str(kind),
        "state": state,
        "session_id": bound_session,
    }


def _worker_heartbeat(
    request: Mapping[str, Any], credentials: PeerCredentials
) -> dict[str, Any]:
    ledger, job_id = _broker_ledger(request)
    _require_worker_contract(request, ledger, job_id)
    return {"verb": "heartbeat", "worker_uid": credentials.uid}


OPERATOR_HANDLERS: Mapping[str, Callable[..., Any]] = MappingProxyType(
    {
        "approve": _operator_approve,
        "reset_change": _operator_reset_change,
        "revise_policy": _operator_revise_policy,
        "enable": _operator_enable,
        "cancel": _operator_cancel,
        "pause": _operator_pause,
        "drain": _operator_drain,
        "resume": _operator_resume,
    }
)

WORKER_HANDLERS: Mapping[str, Callable[..., Any]] = MappingProxyType(
    {
        "report_status": _worker_report_status,
        "request_action": _worker_request_action,
        "record_evidence": _worker_record_evidence,
        "heartbeat": _worker_heartbeat,
        "release_delegated_gate": _worker_release_delegated_gate,
        "report_violation": _worker_report_violation,
    }
)

DISPATCH_TABLES: Mapping[str, Mapping[str, Callable[..., Any]]] = MappingProxyType(
    {
        ENDPOINT_OPERATOR: OPERATOR_HANDLERS,
        ENDPOINT_WORKER: WORKER_HANDLERS,
    }
)


def handler_tables_are_disjoint() -> bool:
    """True when no verb (or handler function) is shared between the endpoints."""
    if set(OPERATOR_HANDLERS) & set(WORKER_HANDLERS):
        return False
    return not (set(OPERATOR_HANDLERS.values()) & set(WORKER_HANDLERS.values()))


# Process-execution primitives that would let a handler run repository-controlled
# code. The privileged service must never execute repo hooks, tests, or commands
# -- they run in the worker domain -- so neither endpoint verb nor handler may
# reach one. Kept as an explicit check so the property is provable rather than
# implied by the handler bodies.
_EXECUTION_PRIMITIVE_NAMES = frozenset(
    {
        "__import__",
        "execv",
        "execve",
        "execvp",
        "execvpe",
        "fork",
        "forkpty",
        "import_module",
        "popen",
        "posix_spawn",
        "posix_spawnp",
        "runpy",
        "spawnv",
        "spawnve",
        "spawnvp",
        "spawnvpe",
        "subprocess",
        "system",
    }
)

# Verb names that would advertise (and likely implement) execution of a
# repository-controlled path. A dispatched verb may not be one of these.
_EXECUTION_VERB_NAMES = frozenset(
    {"command", "eval", "exec", "execute", "hook", "run", "shell", "spawn"}
)


def dispatcher_executes_repo_code(
    tables: Mapping[str, Mapping[str, Callable[..., Any]]] | None = None,
) -> bool:
    """True if any endpoint verb or handler reaches a process-execution primitive.

    Returns ``False`` for the shipped dispatch tables: repository hooks, tests,
    and repo commands execute in the worker domain, never in the privileged
    service, so no repository-controlled path is executed through either
    endpoint. *tables* is an injection seam so the check can be proven against a
    deliberately unsafe table.
    """
    surface = DISPATCH_TABLES if tables is None else tables
    for handlers in surface.values():
        for verb, handler in handlers.items():
            if verb in _EXECUTION_VERB_NAMES or verb in _EXECUTION_PRIMITIVE_NAMES:
                return True
            code = getattr(handler, "__code__", None)
            if code is not None and set(code.co_names) & _EXECUTION_PRIMITIVE_NAMES:
                return True
    return False


@dataclass
class Endpoint:
    """One authenticated end of the operator/worker split.

    ``kind`` selects the disjoint handler table. Only peers whose uid is in
    ``allowed_uids`` pass the accept check. The endpoint exposes no credential
    material: :attr:`credential_material` is always empty and no token file
    mechanism exists.
    """

    kind: str
    allowed_uids: frozenset[int] = field(default_factory=frozenset)

    def __post_init__(self) -> None:
        if self.kind not in DISPATCH_TABLES:
            raise EndpointError(
                f"unknown endpoint kind '{self.kind}'; expected one of "
                f"{', '.join(sorted(DISPATCH_TABLES))}"
            )
        if not isinstance(self.allowed_uids, frozenset):
            self.allowed_uids = frozenset(self.allowed_uids)

    @property
    def handlers(self) -> Mapping[str, Callable[..., Any]]:
        return DISPATCH_TABLES[self.kind]

    @property
    def verbs(self) -> frozenset[str]:
        return frozenset(self.handlers)

    @property
    def credential_material(self) -> dict[str, Any]:
        """Always empty: this scheme has no token/capability file mechanism."""
        return {}

    def resolve(self, verb: str) -> Callable[..., Any]:
        """Return the handler for *verb*, or raise :class:`EndpointError`.

        A verb belonging to the other endpoint is not reachable here.
        """
        handler = self.handlers.get(verb)
        if handler is None:
            raise EndpointError(
                f"verb '{verb}' is not exposed by the {self.kind} endpoint"
            )
        return handler

    def handle(
        self,
        conn: Any,
        *,
        read_request: Callable[[Any], Mapping[str, Any]],
    ) -> dict[str, Any]:
        """Authenticate *conn*, then read and dispatch exactly one request.

        *read_request* is invoked only after the peer is verified, so a
        mismatched peer is closed before any request byte is interpreted.
        """
        credentials = accept_verified_peer(conn, allowed_uids=self.allowed_uids)
        request = read_request(conn)
        if not isinstance(request, Mapping):
            raise EndpointError("request must be a mapping with a 'verb' key")
        verb = request.get("verb")
        if not isinstance(verb, str):
            raise EndpointError("request is missing a string 'verb'")
        handler = self.resolve(verb)
        return handler(request, credentials)


def operator_credential_material_present(endpoint: Endpoint) -> bool:
    """True if the endpoint carries any credential material (never, by design)."""
    return bool(endpoint.credential_material)


__all__ = [
    "DISPATCH_TABLES",
    "ENDPOINT_OPERATOR",
    "ENDPOINT_WORKER",
    "Endpoint",
    "EndpointError",
    "OPERATOR_HANDLERS",
    "PeerCredentialError",
    "PeerCredentials",
    "USES_TOKEN_MATERIAL",
    "WORKER_HANDLERS",
    "accept_verified_peer",
    "dispatcher_executes_repo_code",
    "handler_tables_are_disjoint",
    "operator_credential_material_present",
    "peer_credentials",
]
