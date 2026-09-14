"""Shared orchestration-side supervision helpers.

The CLI needs a service-owned registration signal it can read without
depending on repo-writable files, a broker client to reach the operator and
worker endpoints, and a JSON projection regenerated from broker state so the
legacy read paths keep working.

Design rules enforced here:

- Registration is detected exactly as ``open_supervised_gate`` does: a
  read-only ledger lookup by worktree (``find_job_by_worktree``). JSON markers
  and the repo plan are never consulted for the registration decision.
- An unregistered legacy job takes the legacy path byte-identically and never
  opens or requires a ledger.
- A registered job's mutating commands go through the broker; an unreachable
  broker fails closed with ``BrokerUnavailableError``.
- Dispatch authorization has no in-process bypass. The only evidence that a
  caller may dispatch a registered job is the live, service-owned ledger fence
  (plus the caller's real descent from it); a process-local marker in this
  module is worker-writable and is never consulted.
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from lib.orchestrator import base
from lib.orchestrator import state as state_mod
from lib.supervisor import authority as authority_mod
from lib.supervisor import broker as broker_mod
from lib.supervisor import broker_client
from lib.supervisor import endpoints as endpoints_mod
from lib.supervisor import ledger as ledger_mod
from lib.supervisor import lock as lock_mod

ENV_STATE_FILE = "OPSX_SUPERVISOR_STATE_FILE"


def state_file_configured() -> bool:
    """True when the operator/test store contract explicitly names a store."""
    return bool(os.environ.get(ENV_STATE_FILE, "").strip())


def _authority_service_store() -> Path | None:
    """Return the authority-validated service-owned store path, or ``None``.

    Resolution goes through :func:`lib.supervisor.authority.default_state_path`
    — the service principal's home, or the root-owned system directory — never
    the invoking user's home. The result is canonicalized and must satisfy the
    ledger's trusted-location rule; an untrusted location fails closed.
    """
    try:
        # Resolve the *fixed* service principal, never the caller's
        # environment: ``OPSX_SUPERVISOR_SERVICE_PRINCIPAL`` is worker-writable,
        # so trusting it would let a worker point the "service-owned" store at a
        # home it controls. The provisioning name is the documented default.
        service = authority_mod.resolve_principal(
            "service", authority_mod.DEFAULT_SERVICE_PRINCIPAL
        )
        # Pass an empty env so ``default_state_path`` derives the *service*
        # location rather than echoing back the caller's store override; the
        # override is a separate candidate that detection consults.
        path = authority_mod.default_state_path(env={}, service_uid=service.uid)
    except Exception:
        return None
    try:
        ledger_mod._assert_trusted_location(path)
    except ledger_mod.TrustedLocationError as exc:
        raise broker_mod.BrokerUnavailableError(
            f"the supervision store {path} is not a trusted location: {exc}"
        ) from exc
    return path


def ledger_path(repo: Path | None = None) -> Path | None:
    """Return the configured supervision store path, or the service-owned one.

    An explicit ``OPSX_SUPERVISOR_STATE_FILE`` names the store (provisioning
    and tests use it); otherwise the authority-validated service-owned default
    is used. The result is canonicalized by the authority layer.

    Registration detection does not stop at this path — see
    :func:`ledger_paths` — so a caller cannot hide a registered job by pointing
    the override at a different location.
    """
    if state_file_configured():
        return _canonical_store_path(os.environ.get(ENV_STATE_FILE, "").strip())
    return _authority_service_store()


def _canonical_store_path(configured: str) -> Path:
    """Canonicalize an explicit store path and refuse an untrusted location."""
    path = ledger_mod._canonical(configured)
    try:
        ledger_mod._assert_trusted_location(path)
    except ledger_mod.TrustedLocationError as exc:
        raise broker_mod.BrokerUnavailableError(
            f"the supervision store {path} is not a trusted location: {exc}"
        ) from exc
    return path


def ledger_paths(repo: Path | None = None) -> list[Path]:
    """Return every store candidate registration detection must consult.

    The authority-validated service-owned store is always the *first*
    candidate, even when ``OPSX_SUPERVISOR_STATE_FILE`` names a different path.
    A worker that repoints the override at an empty location cannot hide the
    real registered job, and one that creates a *fake* ledger at the override
    cannot shadow the real one either: the service-owned store wins whenever it
    holds an active job. The configured path remains a candidate (the explicit
    provisioning/test contract) and is consulted when the service store is
    absent, then de-duplicated against it.
    """
    candidates: list[Path] = []
    service_store = _authority_service_store()
    if service_store is not None:
        candidates.append(service_store)
    if state_file_configured():
        candidates.append(_canonical_store_path(os.environ.get(ENV_STATE_FILE, "").strip()))
    deduped: list[Path] = []
    for path in candidates:
        if path not in deduped:
            deduped.append(path)
    return deduped


@dataclass
class Registration:
    """A read-only view of a worktree's active supervised job."""

    ledger: Any
    job_id: int
    job: Any
    policy: dict[str, Any]

    def close(self) -> None:
        try:
            self.ledger.close()
        except Exception:
            pass

    def __enter__(self) -> "Registration":
        return self

    def __exit__(self, *exc: Any) -> None:
        self.close()


def open_registration(repo: Path) -> Registration | None:
    """Return the active registration for *repo*, or ``None``.

    Registration is detected from the authority-validated, service-owned store
    by worktree lookup — never from the repo plan or a JSON marker. No job for
    the worktree or a terminal job yields ``None`` (the legacy path).

    Fail-closed rules for a *substituted* store:

    - an explicitly configured ``OPSX_SUPERVISOR_STATE_FILE`` that does not
      exist is a substitution (a caller could point registration detection at a
      missing path to fall back to legacy JSON), so it raises
      :class:`broker_mod.BrokerUnavailableError` rather than returning ``None``;
    - a store at an untrusted location, or one that exists but cannot be opened
      or read, likewise raises so the caller never silently takes the
      unmediated path.

    The legacy path is reached only when supervision is genuinely not
    provisioned: no candidate store exists (`open_registration` then returns
    ``None``), or an existing candidate holds no active job for the worktree.
    """
    configured_path = (
        _canonical_store_path(os.environ.get(ENV_STATE_FILE, "").strip())
        if state_file_configured()
        else None
    )
    candidates = ledger_paths(repo)
    for path in candidates:
        if not path.exists():
            continue
        registration = _registration_from_store(repo, path)
        if registration is not None:
            return registration
    if configured_path is not None and not configured_path.exists():
        # A configured-but-missing store is a substitution: fail closed rather
        # than let the unmediated legacy path run a registered job. This is the
        # only trust decision the operator -- not the worker -- is expected to
        # set, so a missing configured store is never "unregistered".
        raise broker_mod.BrokerUnavailableError(
            f"the configured supervision store {configured_path} does not exist; a "
            "registered job's store must not be substituted with a missing path "
            "(refusing to fall back to the unmediated legacy path)"
        )
    return None


def _registration_from_store(repo: Path, path: Path) -> Registration | None:
    """Open *path* and return its active job for *repo*, or ``None``."""
    try:
        handle = ledger_mod.open_ledger(path, repository_root=repo, create=False)
    except Exception as exc:
        raise broker_mod.BrokerUnavailableError(
            f"supervision ledger at {path} exists but could not be opened: {exc}"
        ) from exc
    try:
        job = handle.find_job_by_worktree(repo, repository_root=repo)
        if job is None or job["state"] in ledger_mod.TERMINAL_JOB_STATES:
            handle.close()
            return None
        policy = handle.current_policy(int(job["id"]))
    except Exception as exc:
        handle.close()
        raise broker_mod.BrokerUnavailableError(
            f"registered supervised job for {repo} could not be read: {exc}"
        ) from exc
    return Registration(ledger=handle, job_id=int(job["id"]), job=job, policy=policy)


def is_registered(repo: Path) -> bool:
    """True when *repo*'s worktree holds an active supervised job."""
    registration = open_registration(repo)
    if registration is None:
        return False
    registration.close()
    return True


def _live_supervised_fence(ledger: Any, job_id: int) -> Any | None:
    """Return the job's live supervised-execution fence row, or ``None``.

    The authoritative fence lives in the **service-owned ledger** (the
    ``fencing_records`` table), not the repo-writable ``.opsx-plan`` file, so a
    worker cannot forge it. The latest ``acquired`` event whose process
    identity (boot id plus process start time, defeating PID reuse) is still
    live and which has not since been ``released`` or ``fenced`` identifies the
    currently-running supervised execution.
    """
    try:
        rows = ledger.list_fencing(job_id)
    except Exception:
        return None
    current_boot = lock_mod.boot_identity()
    if current_boot is None:
        return None
    latest: Any | None = None
    for row in rows:
        event = row["event"]
        if event == "acquired":
            latest = row
        elif event in ("released", "fenced"):
            if latest is not None and _same_identity(row, latest):
                latest = None
    if latest is None:
        return None
    if latest["boot_id"] != current_boot:
        return None
    pid = latest["pid"]
    start = latest["process_start"]
    if not isinstance(pid, int) or pid <= 0 or start is None:
        return None
    observed = lock_mod.process_start_time(pid)
    if observed is None:
        return None
    try:
        if float(observed) != float(start):
            return None
    except (TypeError, ValueError):
        return None
    return latest


def _same_identity(left: Any, right: Any) -> bool:
    return (
        left["pid"] == right["pid"]
        and left["process_start"] == right["process_start"]
        and left["boot_id"] == right["boot_id"]
    )


def _execution_boundary_reason(registration: Registration, *, pid: int) -> str | None:
    """Return why *registration* does not authorize the current execution.

    Dispatch is authorized only inside a supervised execution the trusted
    service actually started. The evidence is service-owned and unforgeable
    from the worker domain: the ledger's fencing record for this job must name
    a currently-live supervised execution (matching boot and process start
    time, defeating PID reuse) that was never released or fenced, and the
    calling process must be that execution or a descendant of it. A
    worker-exported environment variable is never consulted, because the
    worker controls its environment.

    A missing fence, a released/fenced fence, a stale identity, a different
    boot, or a caller outside the fence's process tree all fail closed.
    """
    try:
        process_start = lock_mod.process_start_time(pid)
    except Exception:
        process_start = None
    if process_start is None:
        return (
            "the current process identity cannot be established (no process "
            "start time), so supervised execution cannot be proven"
        )
    fence = _live_supervised_fence(registration.ledger, registration.job_id)
    if fence is None:
        return (
            "no live supervised-execution fence is recorded in the "
            "service-owned ledger for this job, so an ordinary CLI dispatch "
            "may not run the registered job"
        )
    fence_pid = int(fence["pid"])
    if pid != fence_pid and not _is_descendant_of(pid, fence_pid):
        return (
            f"process {pid} is not a descendant of the supervised execution "
            f"(pid {fence_pid}); an ordinary CLI dispatch may not run a "
            "registered job"
        )
    return None


def _is_descendant_of(pid: int, ancestor_pid: int) -> bool:
    """True when *pid* is *ancestor_pid* or a descendant of it.

    Walks the Linux parent chain via ``/proc``. A process that cannot be read
    or whose ancestry leaves the known chain returns ``False``, so an
    unprovable relationship fails closed.
    """
    if pid == ancestor_pid:
        return True
    seen: set[int] = set()
    current = pid
    for _ in range(64):
        if current in seen:
            return False
        seen.add(current)
        parent = lock_mod.process_parent_pid(current)
        if parent is None or parent <= 0:
            return False
        if parent == ancestor_pid:
            return True
        current = parent
    return False


def require_supervised_authorization(repo: Path) -> Registration | None:
    """Return the registration for a supervised dispatch, or ``None``.

    An unregistered worktree returns ``None`` (the legacy path). A registered
    worktree dispatches only from the supervised execution the trusted service
    started, proven by the live service-owned ledger fence and the caller's
    real process descent from it. An ordinary CLI dispatch outside it —
    including one that merely set a supervised-looking environment variable or
    imported this module's own API — is refused with
    :class:`BrokerMediationError` and the registration handle is closed. The
    caller owns the returned handle.

    A present-but-unreadable backend fails closed for every dispatch
    (``BrokerUnavailableError``), so an unreadable ledger blocks dispatch
    rather than silently running unmediated.
    """
    registration = open_registration(repo)
    if registration is None:
        return None
    reason = _execution_boundary_reason(registration, pid=os.getpid())
    if reason is not None:
        registration.close()
        raise broker_mod.BrokerMediationError(
            "this worktree holds a registered supervised job; dispatch is "
            "broker mediated and only the supervised execution may dispatch: "
            f"{reason}"
        )
    return registration


def gate_resolver(registration: Registration):
    """Return a ``change_id -> bool`` dispatchable resolver over broker receipts."""

    def resolve(change_id: str) -> bool:
        return broker_mod.is_dispatchable(
            registration.ledger, registration.job_id, change_id
        )

    return resolve


def in_supervised_execution(repo: Path | None = None) -> bool:
    """True when this process is the trusted supervised execution.

    The only evidence is the service-owned fence: the job's live supervised
    fencing record must name this process (or an ancestor of it). A
    process-local marker or a worker-exported environment variable is
    deliberately not consulted, so a worker cannot self-authorize by setting
    or importing one. Without *repo* there is no job to prove, so this returns
    ``False``.
    """
    if repo is None:
        return False
    try:
        registration = open_registration(repo)
    except broker_mod.BrokerError:
        return False
    if registration is None:
        return False
    try:
        return _execution_boundary_reason(registration, pid=os.getpid()) is None
    finally:
        registration.close()


# ---------------------------------------------------------------------------
# Broker calls
# ---------------------------------------------------------------------------

# Transport seam. Production resolves the configured Unix socket; a test may
# substitute a real loopback socketpair connector via :func:`set_transport` so
# the client path is exercised over the authority fixtures.
_CLIENT_TRANSPORT: Any = None
_TRANSPORT_PATCHER: Any = None


def set_transport(transport: Any) -> Any:
    """Install a client transport seam, returning the previous one.

    *transport* is called as ``transport(request, kind=..., ...)`` and returns
    the broker result. ``None`` restores the real Unix-socket transport.
    """
    global _CLIENT_TRANSPORT
    previous = _CLIENT_TRANSPORT
    _CLIENT_TRANSPORT = transport
    return previous


def _transport(request: dict[str, Any], *, kind: str) -> dict[str, Any]:
    if _CLIENT_TRANSPORT is not None:
        return _CLIENT_TRANSPORT(request, kind=kind)
    return broker_client.call(request, kind=kind)


def _operator_request(job_id: int, verb: str, **payload: Any) -> dict[str, Any]:
    request = {"verb": verb, "job_id": job_id}
    request.update(payload)
    return request


def call_operator(job_id: int, verb: str, **payload: Any) -> dict[str, Any]:
    """Call the operator endpoint as a client, failing closed on unreachability."""
    return _transport(
        _operator_request(job_id, verb, **payload),
        kind=endpoints_mod.ENDPOINT_OPERATOR,
    )


def call_worker_actions(job_id: int, verb: str, **payload: Any) -> dict[str, Any]:
    """Call the scoped worker-actions endpoint as the registered service."""
    return _transport(
        _operator_request(job_id, verb, **payload),
        kind=endpoints_mod.ENDPOINT_WORKER,
    )


# ---------------------------------------------------------------------------
# Projection
# ---------------------------------------------------------------------------


def project_broker_state(
    repo: Path,
    cfg: dict,
    state: dict,
    ledger: Any,
    job_id: int,
) -> dict:
    """Regenerate the JSON projection from broker and ledger state.

    Approvals and acceptance flags are derived from valid receipts only; the
    projection is write-only from the broker side, so a direct worker edit to
    the JSON has no authority. Reset receipts restore the change's pre-dispatch
    record. The change set is the protected snapshot's registered membership,
    never the repo-writable plan, so a worker cannot hide a change from the
    projection by deleting it from the plan.
    """
    try:
        snapshot = broker_mod.load_protected_snapshot(ledger, job_id)
        projection_order = broker_mod.snapshot_change_ids(snapshot)
    except broker_mod.BrokerError:
        projection_order = list(cfg["order"])
    projection_approvals: list[str] = []
    for cid in projection_order:
        try:
            approval = broker_mod.resolve_gate(ledger, job_id, cid)
        except broker_mod.BrokerError:
            approval = None
        # Only a gated change appears in the approvals projection: an ungated
        # change is trivially dispatchable and legacy JSON never listed it.
        if (
            approval is not None
            and approval.authority is not None
            and approval.dispatchable
        ):
            projection_approvals.append(cid)
        acceptance = _latest_matching_receipt(
            ledger, job_id, cid, broker_mod.ACCEPTANCE
        )
        reset = _latest_matching_receipt(ledger, job_id, cid, broker_mod.RESET)
        record = state_mod.rec(state, cid)
        if acceptance is not None:
            record["accepted"] = True
        if reset is not None and _is_after(reset, acceptance):
            state["changes"][cid] = state_mod.new_change_record()
            state["changes"][cid]["max_rounds"] = cfg["max_rounds"]
            state["changes"][cid]["reason"] = "reset by operator"
            state["changes"][cid]["updated_at"] = base.utcnow()
    state["approvals"] = projection_approvals
    return state


def _latest_matching_receipt(ledger: Any, job_id: int, cid: str, kind: str) -> Any:
    try:
        state = broker_mod.material_state(ledger, job_id, cid)
    except broker_mod.BrokerError:
        return None
    digest = broker_mod.material_hash(
        cid, state.fields, state.snapshot_hash, state.policy_revision
    )
    checkpoint = broker_mod.checkpoint_for(kind, cid)
    for row in reversed(ledger.receipts_for_change(job_id, cid, kind=kind)):
        if row["checkpoint"] == checkpoint and row["material_hash"] == digest:
            return row
    return None


def _is_after(later: Any, earlier: Any) -> bool:
    if earlier is None:
        return True
    return int(later["id"]) > int(earlier["id"])


def persist_projection(repo: Path, cfg: dict, state: dict, ledger: Any, job_id: int) -> None:
    """Regenerate and save the JSON projection for a registered job."""
    project_broker_state(repo, cfg, state, ledger, job_id)
    state_mod.save_state(repo, cfg["name"], state)


__all__ = [
    "ENV_STATE_FILE",
    "Registration",
    "call_operator",
    "call_worker_actions",
    "in_supervised_execution",
    "is_registered",
    "ledger_path",
    "open_registration",
    "persist_projection",
    "project_broker_state",
    "require_supervised_authorization",
    "set_transport",
    "state_file_configured",
]
