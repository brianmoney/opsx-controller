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
import re
import selectors
import shlex
import socket
import subprocess
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Mapping, Sequence

from lib.orchestrator import base
from lib.orchestrator import state as state_mod
from lib.supervisor import authority as authority_mod
from lib.supervisor import broker as broker_mod
from lib.supervisor import broker_client
from lib.supervisor import budgets as budget_mod
from lib.supervisor import endpoints as endpoints_mod
from lib.supervisor import ledger as ledger_mod
from lib.supervisor import lock as lock_mod
from lib.supervisor import model_policy as model_policy_mod
from lib.supervisor import session_bridge as session_bridge_mod
from lib.supervisor import watchdog as watchdog_mod

ENV_STATE_FILE = "OPSX_SUPERVISOR_STATE_FILE"
ENV_SERVER_COMMAND = "OPSX_SESSION_SERVER_COMMAND"


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


# ---------------------------------------------------------------------------
# Read-only supervision projection
# ---------------------------------------------------------------------------

# Bounds so a large ledger never makes an operator surface unbounded.
RECENT_ACTIONS_LIMIT = 10

# An incident that is *not* a completion record indicates rework or a failure
# the change had to be re-entered for; it counts against a correct completion.
_COMPLETED_INCIDENT_KIND = "job_completed"


def _snapshot_projection(snapshot: str | None) -> tuple[dict[str, Any], list[str]]:
    """Return ``(parsed_snapshot, change_ids)`` for a protected snapshot."""
    if not snapshot:
        return {}, []
    try:
        parsed = broker_mod.parse_snapshot(snapshot)
    except broker_mod.BrokerError:
        return {}, []
    return parsed, list(parsed.get("changes", {}))


def _evidence_for_actions(ledger: Any, actions: list[Any]) -> dict[int, list[dict[str, Any]]]:
    """Return evidence rows keyed by action id for *actions* (read-only)."""
    evidence: dict[int, list[dict[str, Any]]] = {}
    for action in actions:
        rows = ledger.list_evidence(int(action["id"]))
        if not rows:
            continue
        evidence[int(action["id"])] = [
            {
                "id": int(row["id"]),
                "kind": str(row["kind"]),
                "payload": row["payload"],
                "recorded_at": row["recorded_at"],
            }
            for row in rows
        ]
    return evidence


def _steering_request_items(
    ledger: Any, job_id: int, *, recent_limit: int
) -> list[dict[str, Any]]:
    """Project request-identified receipts (steering acknowledgements)."""
    return [
        {
            "receipt_id": int(row["id"]),
            "request_id": str(row["request_id"]),
            "kind": str(row["kind"]),
            "change_id": row["change_id"],
            "checkpoint": str(row["checkpoint"]),
            "authority": str(row["authority"]),
            "ack_state": row["ack_state"],
            "ack_boundary": row["ack_boundary"],
            "acked_at": row["acked_at"],
            "created_at": row["created_at"],
        }
        for row in ledger.steering_requests(job_id, limit=recent_limit)
    ]


def _human_wait_briefings(
    ledger: Any,
    job_id: int,
    *,
    changes: Mapping[str, Any],
    recent_actions: list[Any],
    recent_limit: int,
) -> list[dict[str, Any]]:
    """Project the evidence and approval briefing for each open human wait.

    Answers why the job is waiting (the gate and its resolved authority) and
    what authorized the most recent approval, without mutating anything.
    """
    briefings: list[dict[str, Any]] = []
    for wait in ledger.open_waits(job_id, kind="human"):
        change_id = wait["change_id"]
        authority = None
        if change_id:
            try:
                authority = broker_mod.material_state(
                    ledger, job_id, str(change_id)
                ).authority
            except Exception:  # noqa: BLE001 - briefing is advisory
                authority = None
        receipts: list[dict[str, Any]] = []
        if change_id:
            rows = ledger.receipts_for_change(job_id, str(change_id))
            receipts = [
                {
                    "id": int(row["id"]),
                    "kind": str(row["kind"]),
                    "checkpoint": str(row["checkpoint"]),
                    "authority": str(row["authority"]),
                    "actor_principal": row["actor_principal"],
                    "created_at": row["created_at"],
                }
                for row in rows[-recent_limit:]
            ]
        approval = next(
            (item for item in reversed(receipts) if item["kind"] == "approval"),
            None,
        )
        evidence: list[dict[str, Any]] = []
        for action in recent_actions[-recent_limit:]:
            for row in ledger.list_evidence(int(action["id"])):
                payload = row["payload"]
                if change_id and str(change_id) not in str(payload or ""):
                    continue
                evidence.append(
                    {
                        "action_id": int(action["id"]),
                        "id": int(row["id"]),
                        "kind": str(row["kind"]),
                        "payload": payload,
                        "recorded_at": row["recorded_at"],
                    }
                )
        briefings.append(
            {
                "wait": {
                    "id": int(wait["id"]),
                    "kind": str(wait["kind"]),
                    "change_id": change_id,
                    "checkpoint": str(wait["checkpoint"]),
                    "state": str(wait["state"]),
                    "started_at": wait["started_at"],
                    "ended_at": wait["ended_at"],
                },
                "change_id": change_id,
                "checkpoint": str(wait["checkpoint"]),
                "authority": authority,
                "gate": dict(changes.get(str(change_id), {})) if change_id else {},
                "reason": (
                    f"awaiting {authority} approval"
                    if authority
                    else "awaiting operator input"
                ),
                "evidence": evidence[-recent_limit:],
                "receipts": receipts,
                "approval": approval,
            }
        )
    return briefings


def _cost_per_correct_completion(
    *,
    consumption: Mapping[str, Any],
    incidents: list[Any],
    plan_state: Mapping[str, Any] | None,
    change_ids: list[str],
    acceptance_reviews: list[Any],
) -> dict[str, Any]:
    """Define cost-per-correct-completion as a metric, never a promise.

    The value is reconciled supervised cost over the count of changes that
    reached verified completion without a rework incident. The definition,
    its inputs, and its limitations travel with the value so no caller has to
    guess what it measures.
    """
    reconciled = float(consumption.get("reconciled_cost_usd") or 0.0)
    completed = 0
    if isinstance(plan_state, Mapping):
        records = plan_state.get("changes")
        if isinstance(records, Mapping):
            completed = sum(
                1
                for cid in change_ids
                if isinstance(records.get(cid), Mapping)
                and records[cid].get("status") == base.DONE
            )
    if not completed and acceptance_reviews:
        accepted = {
            str(row["change_id"])
            for row in acceptance_reviews
            if str(row["outcome"]) == "accept"
        }
        completed = len(accepted)
    rework_incidents = sum(
        1 for row in incidents if str(row["kind"]) != _COMPLETED_INCIDENT_KIND
    )
    correct = max(completed - rework_incidents, 0)
    value = (reconciled / correct) if correct else None
    return {
        "value": value,
        "definition": (
            "reconciled supervised cost divided by the number of changes that "
            "reached verified completion without a rework incident"
        ),
        "inputs": {
            "reconciled_cost_usd": reconciled,
            "completed_changes": completed,
            "rework_incidents": rework_incidents,
            "correct_completions": correct,
        },
        "limitations": [
            "the numerator counts reconciled cost only; reserved and retained "
            "usage is excluded by definition",
            "the denominator counts changes recorded as done with no rework "
            "incident and is only as complete as the recorded completion evidence",
            "this is a metric definition, not a benchmark, a savings estimate, "
            "or a performance or quality promise",
        ],
    }


def project_job(
    ledger: Any,
    job: Any,
    *,
    repo: Path | None = None,
    plan_name: str | None = None,
    recent_limit: int = RECENT_ACTIONS_LIMIT,
) -> dict[str, Any]:
    """Build the read-only supervision projection for one job.

    Reads the already-open ledger through its read APIs only: it never calls
    the mutating projection path (``project_broker_state``,
    ``persist_projection``, ``save_state``) and never acquires the worktree
    execution lock. Actions and incidents are bounded to the most recent
    *recent_limit* entries so a large ledger stays bounded.
    """
    job_id = int(job["id"])
    policy = ledger.current_policy(job_id)
    snapshot = ledger.current_manifest_snapshot(job_id)
    parsed_snapshot, change_ids = _snapshot_projection(snapshot)
    if plan_name is None:
        plan = parsed_snapshot.get("plan") if isinstance(parsed_snapshot, Mapping) else {}
        if isinstance(plan, Mapping):
            plan_name = plan.get("name")

    manual: dict[str, list[str]] = {}
    if repo is not None:
        for cid in change_ids:
            pending = state_mod.pending_manual_tasks(repo, cid)
            if pending:
                manual[cid] = pending

    actions = ledger.list_actions(job_id)
    recent_actions = actions[-recent_limit:] if recent_limit else []
    incidents = ledger.list_incidents(job_id)
    recent_incidents = incidents[-recent_limit:] if recent_limit else []
    evidence_index = _evidence_for_actions(ledger, recent_actions)
    consumption = ledger.consumption_for_job(job_id)
    steering = _steering_request_items(ledger, job_id, recent_limit=recent_limit)
    human_waits = _human_wait_briefings(
        ledger,
        job_id,
        changes=parsed_snapshot.get("changes", {}) if isinstance(parsed_snapshot, Mapping) else {},
        recent_actions=recent_actions,
        recent_limit=recent_limit,
    )
    plan_state: Mapping[str, Any] | None = None
    if repo is not None and plan_name:
        try:
            plan_state = state_mod.load_state(repo, str(plan_name))
        except Exception:  # noqa: BLE001 - metric is advisory
            plan_state = None
    metrics = _cost_per_correct_completion(
        consumption=consumption,
        incidents=incidents,
        plan_state=plan_state,
        change_ids=change_ids,
        acceptance_reviews=ledger.list_acceptance_reviews(job_id),
    )
    watchdog_state: dict[str, Any] | None = None
    try:
        # Read-only observation: the report computes classification and reads
        # recent events without mutating the ledger, taking the execution lock,
        # or requiring a live service.
        watchdog_state = watchdog_mod.Watchdog(ledger, repo=repo).report(job)
    except Exception:  # noqa: BLE001 - observation is advisory
        watchdog_state = None

    return {
        "job_id": job_id,
        "run_id": job["run_id"],
        "state": str(job["state"]),
        "repo_root": job["repo_root"],
        "worktree": job["worktree_path"],
        "owner": job["owner"],
        "created_at": job["created_at"],
        "updated_at": job["updated_at"],
        "policy": {
            "revision": int(policy["revision"]),
            "policy_version": int(policy["policy_version"]),
            "manifest_snapshot_hash": str(policy["manifest_snapshot_hash"]),
            "budget_policy_state": dict(policy["budget_policy_state"]),
        },
        "budget_posture": {
            "budgets": policy["budgets"],
            "deadlines": policy["deadlines"],
            "consumption": consumption,
        },
        "observed_usage": {
            "states": list(budget_mod.RESERVATION_STATES),
            "totals": consumption,
            "protected_limits": {
                "budgets": policy["budgets"],
                "deadlines": policy["deadlines"],
            },
        },
        "waits": [
            {
                "id": int(row["id"]),
                "kind": str(row["kind"]),
                "change_id": row["change_id"],
                "checkpoint": str(row["checkpoint"]),
                "state": str(row["state"]),
                "started_at": row["started_at"],
                "ended_at": row["ended_at"],
            }
            for row in ledger.list_waits(job_id)
        ],
        "human_waits": human_waits,
        "steering_requests": steering,
        "recent_actions": [
            {
                "id": int(row["id"]),
                "action_id": int(row["id"]),
                "run_id": row["run_id"],
                "kind": str(row["kind"]),
                "state": str(row["state"]),
                "updated_at": row["updated_at"],
                "evidence": evidence_index.get(int(row["id"]), []),
            }
            for row in recent_actions
        ],
        "recent_incidents": [
            {
                "id": int(row["id"]),
                "incident_id": int(row["id"]),
                "kind": str(row["kind"]),
                "state": str(row["state"]),
                "signature": row["signature"],
                "summary": row["summary"],
                "created_at": row["created_at"],
            }
            for row in recent_incidents
        ],
        "linkage_config": ledger.job_linkage_config(job_id),
        "pending_manual_tasks": manual,
        "watchdog": watchdog_state,
        "metrics": {
            "cost_per_correct_completion": metrics,
        },
    }


def project_registered_job(
    repo: Path,
    *,
    plan_name: str | None = None,
    recent_limit: int = RECENT_ACTIONS_LIMIT,
) -> dict[str, Any] | None:
    """Return the read-only projection for *repo*, or ``None`` when unregistered.

    Observation includes a worktree's most recent terminal job as well as an
    active one: a completed or cancelled job still has a durable supervision
    record an operator should be able to inspect. The ledger is opened with
    ``create=False`` and the projection never mutates it, so an unregistered
    worktree opens nothing and its operator output stays byte-identical.
    """
    registration = _observability_registration(repo)
    if registration is None:
        return None
    try:
        return project_job(
            registration.ledger,
            registration.job,
            repo=repo,
            plan_name=plan_name,
            recent_limit=recent_limit,
        )
    finally:
        registration.close()


def _observability_registration(repo: Path) -> Registration | None:
    """Return the worktree's projection target, including a terminal job.

    Unlike :func:`open_registration`, which gates mutating commands and
    deliberately ignores a terminal job, observability resolves the active job
    or the most recent terminal one. No job for the worktree yields ``None``.
    """
    for path in ledger_paths(repo):
        if not path.exists():
            continue
        try:
            handle = ledger_mod.open_ledger(
                path, repository_root=repo, create=False
            )
        except Exception as exc:  # noqa: BLE001 - named fail-closed error
            raise broker_mod.BrokerUnavailableError(
                f"supervision ledger at {path} exists but could not be opened: {exc}"
            ) from exc
        try:
            job = handle.find_job_by_worktree(repo, repository_root=repo)
            if job is None:
                handle.close()
                continue
            policy = handle.current_policy(int(job["id"]))
        except Exception as exc:  # noqa: BLE001 - named fail-closed error
            handle.close()
            raise broker_mod.BrokerUnavailableError(
                f"supervised job for {repo} could not be read: {exc}"
            ) from exc
        return Registration(
            ledger=handle, job_id=int(job["id"]), job=job, policy=policy
        )
    return None


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


def execution_boundary_reason(ledger: Any, job_id: int, *, pid: int | None = None) -> str | None:
    """Return why *pid* is not the job's live supervised execution, or ``None``.

    This is the durable, unforgeable execution-lock evidence a dispatch
    boundary revalidates per action: the service-owned ledger fencing record
    for *job_id* must name a currently-live supervised execution (matching
    boot identity and process start time, defeating PID reuse) that was never
    released or fenced, and the process must be that execution or a descendant
    of it. The repo-writable ``.opsx-plan`` lock record is never trusted for
    this decision, so a worker cannot forge a held worktree lock.
    """
    registration = Registration(ledger=ledger, job_id=int(job_id), job=None, policy={})
    return _execution_boundary_reason(
        registration, pid=os.getpid() if pid is None else int(pid)
    )


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


def install_projection_writer(repo: Path, cfg: dict) -> Any:
    """Install the trusted-service projection writer into the broker.

    The supervised service calls this once at boot so every committed broker
    transaction regenerates the JSON projection from broker and ledger state.
    The writer reloads state from disk, projects it, and saves it, so the
    projection reflects the freshly recorded receipts. Returns the previously
    installed writer so a caller can restore it.

    This is deliberately not a per-call callback: the broker consults the
    installed writer itself, so an endpoint-recorded receipt cannot silently
    leave the projection stale.
    """
    def writer(ledger: Any, job_id: int) -> None:
        state = state_mod.load_state(repo, cfg["name"])
        persist_projection(repo, cfg, state, ledger, job_id)

    return broker_mod.set_projection_writer(writer)


@dataclass
class ServiceSession:
    """The trusted service-side broker session (production bootstrap).

    A session is the one production route through which endpoint requests are
    accepted. It owns the service-owned ledger handle, the identified job, and
    the trusted projection writer the service installed at boot, so every
    committed receipt transaction regenerates the JSON projection before the
    request completes. Closing the session restores the previously installed
    writer and closes the ledger.
    """

    repo: Path
    cfg: dict
    job_id: int
    ledger: Any
    previous_writer: Any

    def serve(
        self,
        endpoint: Any,
        conn: Any,
        *,
        ledger_resolver: Any = None,
    ) -> dict[str, Any]:
        """Accept and dispatch exactly one broker request on *conn*.

        The request never carries the service-owned ledger: the session
        injects its own ledger handle and job id, exactly as the supervised
        service does, so a worker cannot substitute the ledger over the wire.
        """
        resolver = ledger_resolver or (
            lambda _request: (self.ledger, self.job_id)
        )
        return broker_client.serve_one(endpoint, conn, ledger_resolver=resolver)

    def close(self) -> None:
        try:
            broker_mod.set_projection_writer(self.previous_writer)
        finally:
            try:
                self.ledger.close()
            except Exception:
                pass

    def __enter__(self) -> "ServiceSession":
        return self

    def __exit__(self, *exc: Any) -> None:
        self.close()


def open_service_session(
    repo: Path,
    cfg: dict,
    *,
    job_id: int | None = None,
    store_path: Path | None = None,
) -> ServiceSession:
    """Boot the trusted service-side broker session for *repo*.

    This is the production bootstrap the supervised service runs before
    accepting any operator or worker endpoint request:

    1. open the service-owned ledger (never the repo-writable state),
    2. identify this worktree's active nonterminal registration and bind the
       session to it — an explicit *job_id* is only accepted when it *is* that
       registration, so a foreign job id is rejected rather than served,
    3. install and retain the trusted projection writer via
       :func:`install_projection_writer`, so every receipt transaction
       regenerates the JSON projection from broker and ledger state.

    A missing, corrupt, or otherwise unopenable service store, a worktree with
    no active job, or an explicit *job_id* that does not match the worktree's
    active registration fails closed with
    :class:`broker_mod.BrokerUnavailableError` before the writer is installed,
    rather than starting a session that could record authority for a foreign
    job with no projection or leak a raw ledger/database error. The returned
    session is a context manager; the caller owns it.
    """
    path = Path(store_path) if store_path is not None else ledger_path(repo)
    if path is None:
        raise broker_mod.BrokerUnavailableError(
            "no service-owned supervision store is configured; the supervised "
            "service cannot accept broker requests"
        )
    if not path.exists():
        raise broker_mod.BrokerUnavailableError(
            f"the service-owned supervision store {path} does not exist; the "
            "supervised service must be provisioned before accepting broker "
            "requests"
        )
    try:
        # An existing but unopenable store (corrupt file, unsupported schema,
        # untrusted location) is an unavailable service store like any other:
        # it must fail closed as the named broker error, never leak a raw
        # ``sqlite3.DatabaseError``/``LedgerError`` out of the bootstrap. The
        # open precedes writer installation, so a failed open leaves no
        # session, no writer, and nothing recorded or projected.
        handle = ledger_mod.open_ledger(path, repository_root=repo, create=False)
    except broker_mod.BrokerUnavailableError:
        raise
    except Exception as exc:
        raise broker_mod.BrokerUnavailableError(
            f"the service-owned supervision store {path} exists but could not be "
            f"opened; the supervised service is unavailable and must be "
            f"reprovisioned before accepting broker requests: {exc}"
        ) from exc
    try:
        # Every session, including an explicit ``--job-id``, is bound to *this*
        # worktree's active nonterminal registration before anything is
        # installed or bound. An explicit id that names another worktree's job
        # (or a terminal job) is a mismatch and is rejected here: otherwise the
        # service would record receipts for a foreign job while projecting into
        # the current repo.
        active = handle.find_job_by_worktree(repo, repository_root=repo)
        if active is None or active["state"] in ledger_mod.TERMINAL_JOB_STATES:
            raise broker_mod.BrokerUnavailableError(
                f"worktree {repo} has no active supervised job; refusing to "
                "open a broker session for an unregistered worktree"
            )
        active_id = int(active["id"])
        if job_id is None:
            job_id = active_id
        elif int(job_id) != active_id:
            raise broker_mod.BrokerUnavailableError(
                f"job {job_id} is not the active supervised job {active_id} "
                f"registered for worktree {repo}; refusing to serve a job that "
                "belongs to another worktree"
            )
        previous = install_projection_writer(repo, cfg)
    except Exception:
        handle.close()
        raise
    return ServiceSession(
        repo=repo,
        cfg=cfg,
        job_id=job_id,
        ledger=handle,
        previous_writer=previous,
    )


OPERATOR_SOCKET_ENV = broker_client.OPERATOR_SOCKET_ENV
WORKER_SOCKET_ENV = broker_client.WORKER_SOCKET_ENV


def service_socket_path(kind: str, store_path: Path) -> Path:
    """Return the service-owned endpoint socket path for *kind*.

    An explicit socket env override wins (that is the provisioning/test
    contract the CLI client reads too); otherwise the socket lives beside the
    service-owned store so a worker cannot repoint it. The result is
    canonicalized alongside the store.
    """
    configured = broker_client.endpoint_socket_path(kind)
    if configured is not None:
        return configured
    name = "operator.sock" if kind == endpoints_mod.ENDPOINT_OPERATOR else "worker.sock"
    return store_path.parent / name


def _allowed_uids_for(role: str, *, principal: Any = None) -> frozenset[int]:
    """Return the kernel uids the *role* endpoint accepts.

    The authenticated principal is resolved through
    :mod:`lib.supervisor.authority` (never a worker-selectable name); a
    missing, unresolvable principal fails closed with
    :class:`broker_mod.BrokerUnavailableError`, because an endpoint that
    cannot name its allowed peer must not accept one.
    """
    if principal is not None and principal.uid is not None:
        return frozenset({int(principal.uid)})
    name = (
        authority_mod.DEFAULT_SERVICE_PRINCIPAL
        if role == "service"
        else None
    )
    if name is None:
        raise broker_mod.BrokerUnavailableError(
            f"no principal is provisioned for the {role} endpoint; refusing to "
            "accept unauthenticated broker requests"
        )
    try:
        resolved = authority_mod.resolve_principal(role, name)
    except Exception as exc:  # pragma: no cover - authority lookup failure
        raise broker_mod.BrokerUnavailableError(
            f"the {role} principal could not be resolved: {exc}"
        ) from exc
    if resolved.uid is None:
        raise broker_mod.BrokerUnavailableError(
            f"the {role} principal {name!r} is not provisioned on this host; "
            "refusing to accept unauthenticated broker requests"
        )
    return frozenset({int(resolved.uid)})


@dataclass
class ServiceEndpointHost:
    """Production host that serves authenticated broker endpoint requests.

    The host is created from a booted :class:`ServiceSession` (which has
    already installed and retained the trusted projection writer), binds the
    operator and worker-actions Unix sockets with service-derived allowed
    peer uids, and dispatches each accepted connection through the session.
    Because the session installs the writer before any request is accepted,
    a live endpoint receipt transaction always records *and* regenerates the
    JSON projection.
    """

    session: ServiceSession
    store_path: Path
    operator_allowed_uids: frozenset[int]
    service_allowed_uids: frozenset[int]
    operator_socket: Path
    worker_socket: Path
    primary_session: "PrimarySessionRuntime | None" = None
    _listeners: dict[str, Any] = None  # type: ignore[assignment]
    _stopped: bool = False
    watchdog: Any = None
    watchdog_interval: float = 5.0
    _watchdog_scanned: bool = False
    _last_watchdog_tick: float = 0.0
    watchdog_error: str | None = None

    def __post_init__(self) -> None:
        if self._listeners is None:
            self._listeners = {}

    # -- watchdog loop -----------------------------------------------------

    def run_boot_scan(self) -> Any:
        """Reconcile every non-terminal job before serving.

        The boot scan records each job's classification and attempts reconnect
        before any respawn. A watchdog failure is contained: the endpoint
        surface must still serve requests, so the error is recorded on the host
        rather than escaping the serve loop.
        """
        if self.watchdog is None:
            return None
        self._watchdog_scanned = True
        self._last_watchdog_tick = time.monotonic()
        try:
            return self.watchdog.boot_scan()
        except Exception as exc:  # noqa: BLE001 - contained, recorded below
            self.watchdog_error = str(exc)
            return None

    def maybe_tick(self) -> Any:
        """Run a watchdog tick when the configured interval has elapsed."""
        if self.watchdog is None:
            return None
        now = time.monotonic()
        if now - self._last_watchdog_tick < float(self.watchdog_interval):
            return None
        self._last_watchdog_tick = now
        try:
            return self.watchdog.tick()
        except Exception as exc:  # noqa: BLE001 - contained, recorded below
            self.watchdog_error = str(exc)
            return None

    def start_primary_session(
        self,
        *,
        title: str | None = None,
        agent: str | None = None,
        model: Mapping[str, Any] | None = None,
        launcher: Any = None,
        transport_factory: Any = None,
    ) -> "PrimarySessionRuntime":
        """Start or adopt this job's service-managed primary session.

        The host owns the lifetime: it records the linkage, keeps the runtime,
        and tears it down on :meth:`close`. A second call returns the existing
        runtime (a service owns one primary session per job).
        """
        if self.primary_session is not None:
            return self.primary_session
        self.primary_session = open_primary_session(
            Path(self.session.repo),
            self.session,
            title=title,
            agent=agent,
            model=model,
            launcher=launcher,
            transport_factory=transport_factory,
        )
        return self.primary_session

    def _endpoint(self, kind: str) -> Any:
        endpoints_map = {
            endpoints_mod.ENDPOINT_OPERATOR: (
                self.operator_allowed_uids, self.operator_socket
            ),
            endpoints_mod.ENDPOINT_WORKER: (
                self.service_allowed_uids, self.worker_socket
            ),
        }
        allowed, _path = endpoints_map[kind]
        return endpoints_mod.Endpoint(kind, allowed)

    def bind(self) -> "ServiceEndpointHost":
        """Bind and listen on both endpoint sockets.

        A stale socket file is removed first; the socket is created with
        restrictive permissions (owner-only) so a worker cannot connect by
        filesystem permission even before its uid is rejected.

        Every socket provisioning failure (creating, preparing, binding, or
        listening) is translated to
        :class:`broker_mod.BrokerUnavailableError`, the named fail-closed error
        the ``supervise serve`` contract promises: an unbindable endpoint must
        not escape as a raw ``OSError``. On failure the partially bound sockets
        are closed and the session (including the projection writer it
        installed) is torn down before the error propagates, so a failed boot
        leaves no half-installed authority surface behind.
        """
        for kind, path in (
            (endpoints_mod.ENDPOINT_OPERATOR, self.operator_socket),
            (endpoints_mod.ENDPOINT_WORKER, self.worker_socket),
        ):
            path = Path(path)
            try:
                path.parent.mkdir(parents=True, exist_ok=True)
                if path.exists():
                    path.unlink()
            except OSError as exc:
                self.close()
                raise broker_mod.BrokerUnavailableError(
                    f"the {kind} endpoint socket {path} could not be prepared; "
                    f"refusing to serve broker requests: {exc}"
                ) from exc
            try:
                listener = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
            except OSError as exc:  # pragma: no cover - platform dependent
                self.close()
                raise broker_mod.BrokerUnavailableError(
                    f"the {kind} endpoint socket could not be created; refusing "
                    f"to serve broker requests: {exc}"
                ) from exc
            try:
                listener.bind(str(path))
                try:
                    os.chmod(path, 0o600)
                except OSError:  # pragma: no cover - platform dependent
                    pass
                listener.listen(8)
            except OSError as exc:
                try:
                    listener.close()
                except OSError:  # pragma: no cover - best-effort close
                    pass
                try:
                    if path.exists() and path.is_socket():
                        path.unlink()
                except OSError:  # pragma: no cover - best-effort cleanup
                    pass
                self.close()
                raise broker_mod.BrokerUnavailableError(
                    f"the {kind} endpoint socket {path} could not be bound; "
                    f"refusing to serve broker requests: {exc}"
                ) from exc
            self._listeners[kind] = (listener, path)
        return self

    def poll(self, timeout: float | None = None) -> dict[str, Any] | None:
        """Accept and dispatch at most one request, returning its result.

        Blocks up to *timeout* seconds for a connection; a timeout returns
        ``None`` with nothing dispatched.
        """
        if not self._listeners:
            raise broker_mod.BrokerUnavailableError(
                "the service endpoint host is not bound; call bind() first"
            )
        # Boot reconciliation runs before the first request is served; a
        # periodic tick runs when its interval has elapsed. Both are contained
        # so the broker endpoint surface keeps serving.
        if not self._watchdog_scanned:
            self.run_boot_scan()
        self.maybe_tick()
        listener_map = {
            listener: kind for kind, (listener, _p) in self._listeners.items()
        }
        selector = selectors.DefaultSelector()
        try:
            for listener, _path in self._listeners.values():
                selector.register(listener, selectors.EVENT_READ)
            events = selector.select(timeout)
        finally:
            selector.close()
        for key, _mask in events:
            listener = key.fileobj
            kind = listener_map[listener]
            conn, _addr = listener.accept()
            try:
                return self.session.serve(self._endpoint(kind), conn)
            except endpoints_mod.PeerCredentialError as exc:
                # A rejected peer is closed by the endpoint; surface it so the
                # caller can decide whether the refusal is fatal.
                return {"ok": False, "error": "PeerCredentialError", "message": str(exc)}
        return None

    def serve_forever(self, *, timeout: float = 1.0) -> None:
        """Poll for and dispatch endpoint requests until :meth:`stop`."""
        while not self._stopped:
            self.poll(timeout)

    def stop(self) -> None:
        self._stopped = True

    def close(self) -> None:
        if self.primary_session is not None:
            try:
                self.primary_session.close()
            except Exception:  # pragma: no cover - best-effort teardown
                pass
            self.primary_session = None
        for listener, path in self._listeners.values():
            try:
                listener.close()
            except OSError:  # pragma: no cover - best-effort close
                pass
            try:
                if isinstance(path, Path) and path.exists():
                    path.unlink()
            except OSError:  # pragma: no cover - best-effort cleanup
                pass
        self._listeners = {}
        self.watchdog = None
        try:
            self.session.close()
        except Exception:
            pass

    def __enter__(self) -> "ServiceEndpointHost":
        return self.bind()

    def __exit__(self, *exc: Any) -> None:
        self.close()


def _watchdog_session_is_live(ledger: Any, job_id: int) -> bool:
    """Return whether the job's recorded primary session is still adoptable.

    A read-only lookup through the documented session bridge: it probes the
    recorded server address and asks for the recorded session. A missing
    linkage, an unreachable server, or an absent session is not live, so the
    watchdog falls through to the (quiescence-gated) reconstitution path.
    """
    linkage = session_bridge_mod.primary_session_linkage(ledger, int(job_id))
    if linkage is None:
        return False
    transport = None
    try:
        transport = session_bridge_mod.LoopbackTransport.from_address(
            linkage.server_address
        )
        bridge = session_bridge_mod.SessionBridge(transport)
        bridge.check_capability()
        return bridge.lookup_session(linkage.session_id) is not None
    except Exception:  # noqa: BLE001 - an unreachable probe is simply not live
        return False
    finally:
        if transport is not None:
            try:
                transport.close()
            except Exception:  # pragma: no cover - best-effort close
                pass


def open_service_host(
    repo: Path,
    cfg: dict,
    *,
    store_path: Path | None = None,
    job_id: int | None = None,
    operator_principal: Any = None,
    service_principal: Any = None,
    watchdog_redrive: Any = None,
) -> ServiceEndpointHost:
    """Boot the production service host: session + projection writer + sockets.

    This is the single production call site for
    :func:`install_projection_writer`: the writer is installed by the session
    before the host binds, so every endpoint request served by the host
    records its receipt and regenerates the JSON projection. A missing store,
    an unregistered worktree, or an unresolvable allowed principal fails
    closed before any socket is bound.

    *operator_principal* / *service_principal* are test seams that override
    the authority layer's resolved endpoints; production omits them and
    resolves the operator and service principals from the OS.
    """
    session = open_service_session(repo, cfg, store_path=store_path, job_id=job_id)
    try:
        effective_store = Path(store_path) if store_path is not None else ledger_path(repo)
        if effective_store is None:
            raise broker_mod.BrokerUnavailableError(
                "no service-owned supervision store is configured"
            )
        operator_allowed = (
            _allowed_uids_for("operator", principal=operator_principal)
            if operator_principal is not None
            else _operator_allowed_uids()
        )
        service_allowed = _allowed_uids_for("service", principal=service_principal)
        watchdog = watchdog_mod.Watchdog(
            session.ledger,
            repo=repo,
            reconnect=_watchdog_session_is_live,
            redrive=watchdog_redrive,
        )
        return ServiceEndpointHost(
            session=session,
            store_path=effective_store,
            operator_allowed_uids=operator_allowed,
            service_allowed_uids=service_allowed,
            operator_socket=service_socket_path(
                endpoints_mod.ENDPOINT_OPERATOR, effective_store
            ),
            worker_socket=service_socket_path(
                endpoints_mod.ENDPOINT_WORKER, effective_store
            ),
            watchdog=watchdog,
        )
    except Exception:
        session.close()
        raise


def _operator_allowed_uids() -> frozenset[int]:
    """Return the operator principal's uid resolved from the OS authority layer."""
    report = authority_mod.detect_backend()
    principals = report.principals
    if principals is None or principals.operator.uid is None:
        raise broker_mod.BrokerUnavailableError(
            "the operator principal is not provisioned on this host; refusing "
            "to accept unauthenticated broker requests"
        )
    return frozenset({int(principals.operator.uid)})


# ---------------------------------------------------------------------------
# Service-owned primary session lifetime
# ---------------------------------------------------------------------------


def session_server_command(job_id: int | None = None) -> list[str]:
    """Return the worker-domain headless session server command.

    The service launches the headless server through this argv in the worker
    domain; the restricted-process launcher (not a new endpoint verb) carries
    the identity switch, keeping the endpoint tables' no-execution contract
    intact. An operator may pin the argv explicitly through
    ``OPSX_SESSION_SERVER_COMMAND``; otherwise the documented ``opencode
    serve`` invocation runs, bound to a **job-specific loopback port** derived
    deterministically from the job id so the job's server and session are
    identifiable and adoptable after a restart.
    """
    configured = (os.environ.get(ENV_SERVER_COMMAND) or "").strip()
    if configured:
        return shlex.split(configured)
    return [
        "opencode", "serve", "--hostname", "127.0.0.1",
        "--port", str(job_loopback_port(job_id)),
    ]


# The job-specific loopback port range: a deterministic, per-job binding in an
# otherwise-unassigned high range, so a job's server address is reproducible
# across service restarts (which adopt-by-lookup needs).
LOOPBACK_PORT_BASE = 41000
LOOPBACK_PORT_SPAN = 2000


def job_loopback_port(job_id: int | None) -> int:
    """Return the deterministic job-specific loopback port for *job_id*."""
    if job_id is None:
        return 0  # ephemeral: the server reports its own address
    return LOOPBACK_PORT_BASE + (int(job_id) % LOOPBACK_PORT_SPAN)


def _server_address_from_line(line: str) -> str | None:
    """Extract a loopback ``host:port`` from a server startup line, or ``None``.

    The headless server prints its bound address (``opencode server listening
    on http://127.0.0.1:PORT``); a non-loopback or portless line is refused so
    the bridge can never be pointed off-loopback.
    """
    match = re.search(r"(127\.0\.0\.1|\[::1\]|localhost):(\d+)", line or "")
    if match is None:
        return None
    host, port = match.group(1), match.group(2)
    return f"{host.strip('[]')}:{port}"


def launch_session_server(
    *,
    job_id: int | None = None,
    switch: Sequence[str] | None = None,
    command: Sequence[str] | None = None,
    popen_factory: Any = None,
    boot_timeout: float = 20.0,
    env: Mapping[str, str] | None = None,
) -> "SessionServerProcess":
    """Launch the job's headless session server in the worker domain.

    The server runs under the worker principal exactly like any other model
    session: the argv is prefixed with the authenticated restricted-process
    launcher from the authority boundary, never with service-identity
    privileges. Its process identity is journaled by the caller through the
    normal dispatch/identity surface, so the existing fencing machinery can
    tell a live server from a recycled PID. *job_id* selects the deterministic
    job-specific loopback port; *popen_factory* is a test seam.
    """
    argv = (
        list(command)
        if command is not None
        else session_server_command(job_id)
    )
    mechanism = list(switch) if switch is not None else authority_mod.discover_switch_mechanism(
        authority_mod.DEFAULT_WORKER_PRINCIPAL, env=env
    )
    if not mechanism:
        raise broker_mod.BrokerUnavailableError(
            "no restricted-spawn mechanism is available to launch the headless "
            f"session server under the worker principal "
            f"'{authority_mod.DEFAULT_WORKER_PRINCIPAL}'; refusing to run a "
            "model session with service-identity privileges"
        )
    mechanism = authority_mod.canonical_switch_mechanism(mechanism)
    full_argv = mechanism + argv
    factory = popen_factory or subprocess.Popen

    def spawner(argv: Sequence[str]) -> Any:
        return factory(
            list(argv), stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True
        )

    try:
        process = spawner(full_argv)
    except Exception as exc:  # noqa: BLE001 - every spawn failure is named
        raise broker_mod.BrokerUnavailableError(
            f"the headless session server could not be started in the worker "
            f"domain: {exc}"
        ) from exc
    server = SessionServerProcess(
        process=process, command=tuple(full_argv), worker_command=tuple(argv)
    )
    try:
        server.await_address(timeout=boot_timeout)
    except Exception:
        server.terminate()
        raise
    return server


@dataclass
class SessionServerProcess:
    """A service-owned headless session server running in the worker domain."""

    process: Any
    command: tuple[str, ...]
    worker_command: tuple[str, ...]
    address: str | None = None
    _reader: Any = None

    @property
    def pid(self) -> int:
        return int(self.process.pid)

    @property
    def process_identity(self) -> str:
        """The fenceable pid + start time + boot identity of the server."""
        return session_bridge_mod.serialize_process_identity(self.pid)

    def await_address(self, *, timeout: float = 20.0) -> str:
        """Read the server's bound loopback address, or fail closed."""
        if self.address is not None:
            return self.address
        stream = getattr(self.process, "stdout", None)
        if stream is None:
            raise broker_mod.BrokerUnavailableError(
                "the headless session server exposes no startup stream; its "
                "loopback address cannot be established"
            )
        deadline = time.monotonic() + float(timeout)
        while time.monotonic() < deadline:
            line = stream.readline()
            if not line:
                break
            address = _server_address_from_line(line)
            if address is not None:
                self.address = address
                return address
        raise broker_mod.BrokerUnavailableError(
            "the headless session server did not report a loopback address "
            "before the startup timeout; refusing to adopt an unaddressed session"
        )

    def terminate(self) -> None:
        try:
            self.process.terminate()
        except Exception:  # pragma: no cover - best-effort termination
            pass


def open_primary_session(
    repo: Path,
    session: "ServiceSession",
    *,
    title: str | None = None,
    agent: str | None = None,
    model: Mapping[str, Any] | None = None,
    launcher: Any = None,
    transport_factory: Any = None,
    deliver_briefing: bool = True,
    briefing_max_attempts: int = budget_mod.BACKOFF_MAX_ATTEMPTS,
) -> "PrimarySessionRuntime":
    """Start or adopt the job's service-managed primary session.

    Adopt-by-lookup runs first: when the job's recorded primary session linkage
    resolves through lookup on the recorded server, the live session is adopted
    (and receives a bounded re-brief) instead of a replacement being launched.
    Only when lookup shows the session is gone does a replacement server and
    session get created from a full bounded briefing. Either way the linkage is
    journaled, so an operator's interactive chat starts or attaches to the same
    service-managed session.

    The returned runtime drives every primary prompt through a
    :class:`~lib.supervisor.session_bridge.JournaledSessionBridge` built from
    the recorded policy and the headless server's process identity: the prompt
    intent (with its request identity) is journaled before the server request,
    a supervisor-role budget reservation is committed first, a dispatch record
    binds the session and server process identity, and observed usage is
    reconciled after the terminal result. The bounded full/rebrief briefing is
    dispatched through that same bridge before the runtime is returned, so the
    managed session is actually briefed rather than merely describing one.

    The primary session is bound to its concrete `opsx-supervisor` agent
    through the journaled bridge, which runs the session contract first: a
    caller-supplied *agent* that is not the supervisor role's registered agent
    is refused with a recorded `policy_violation` rather than run. The session
    is created under the supervisor role's exact pinned model — resolved from
    the job policy when the caller supplies none — so an unpinned session can
    never reach the server.
    """
    ledger = session.ledger
    job_id = int(session.job_id)
    change_id = session_change_id(session)
    try:
        run_id = str(ledger.get_job(job_id)["run_id"])
    except Exception:  # noqa: BLE001 - a missing job row is durable-state absence
        run_id = str(job_id)
    try:
        policy = ledger.current_policy(job_id)
    except Exception:  # noqa: BLE001 - absent policy is durable-state absence
        policy = None
    prompt_plan = _supervisor_prompt_plan(repo, policy)
    transport_factory = transport_factory or session_bridge_mod.LoopbackTransport.from_address
    if linkage := session_bridge_mod.primary_session_linkage(ledger, job_id):
        transport = None
        try:
            transport = transport_factory(linkage.server_address)
            bridge = session_bridge_mod.SessionBridge(transport)
            bridge.check_capability()
            live = bridge.lookup_session(linkage.session_id)
        except session_bridge_mod.SessionBridgeError:
            live = None
        if live is not None:
            briefing = session_bridge_mod.compose_briefing(
                ledger, job_id, mode="rebrief",
                change_id=change_id,
            )
            runtime = PrimarySessionRuntime(
                bridge=bridge,
                session_id=linkage.session_id,
                server_address=linkage.server_address,
                adopted=True,
                briefing=briefing,
                linkage=linkage,
                journaled=_journaled_session_bridge(
                    ledger, job_id, run_id, bridge,
                    policy=policy,
                    process_id=linkage.process_id,
                    change_id=change_id,
                ),
                prompt_plan=prompt_plan,
            )
            try:
                _apply_briefing(
                    runtime,
                    model=model,
                    agent=agent,
                    max_attempts=briefing_max_attempts,
                    enabled=deliver_briefing,
                )
            except Exception:
                # An adopted session that cannot receive its re-brief is not
                # handed back half-briefed; its probe connection is released.
                runtime.close()
                raise
            return runtime
        # Lookup showed the session is gone: the probe connection is not the
        # adopted runtime, so it is released before a replacement is launched.
        if transport is not None:
            try:
                transport.close()
            except Exception:  # pragma: no cover - best-effort close
                pass
    server_launcher = launcher or (
        lambda: launch_session_server(job_id=job_id)
    )
    server = server_launcher()
    try:
        transport = transport_factory(server.address)
        bridge = session_bridge_mod.SessionBridge(transport)
        bridge.check_capability()
        journaled = _journaled_session_bridge(
            ledger, job_id, run_id, bridge,
            policy=policy,
            process_id=server.process_identity,
            change_id=change_id,
        )
        # The primary session is bound to its registered concrete agent through
        # the journaled bridge, so the session contract is checked (and a
        # spoil/escalation recorded as a policy_violation) before the session
        # exists, rather than trusting a caller-supplied agent. The session is
        # created under the supervisor role's exact pin: when the caller does
        # not supply one, the service resolves its own pinned identity from the
        # job policy (a caller-supplied model that differs from the pin is still
        # refused, never substituted).
        session_model = model
        if session_model is None:
            planned = prompt_plan.get("model")
            session_model = planned if isinstance(planned, Mapping) else None
        created = journaled.create_session(
            title=title,
            role=model_policy_mod.SUPERVISOR_ROLE,
            model=session_model,
        )
        session_id = str(created["id"])
    except Exception:
        try:
            server.terminate()
        except Exception:  # pragma: no cover - best-effort cleanup
            pass
        raise
    briefing = session_bridge_mod.compose_briefing(
        ledger, job_id, mode="full", change_id=change_id
    )
    linkage = session_bridge_mod.record_primary_session_linkage(
        ledger,
        job_id,
        run_id=run_id,
        server_address=str(server.address),
        session_id=session_id,
        process_id=server.process_identity,
    )
    runtime = PrimarySessionRuntime(
        bridge=bridge,
        session_id=session_id,
        server_address=str(server.address),
        adopted=False,
        briefing=briefing,
        linkage=linkage,
        journaled=journaled,
        prompt_plan=prompt_plan,
        server=server,
    )
    try:
        _apply_briefing(
            runtime,
            model=model,
            agent=agent,
            max_attempts=briefing_max_attempts,
            enabled=deliver_briefing,
        )
    except Exception:
        # A replacement whose briefing cannot be dispatched is not a usable
        # primary: tear the server down rather than returning a session that
        # was never briefed (and would leak a headless server).
        runtime.close()
        raise
    return runtime


def _journaled_session_bridge(
    ledger: Any,
    job_id: int,
    run_id: str,
    bridge: Any,
    *,
    policy: Mapping[str, Any] | None,
    process_id: str | None,
    change_id: str | None,
) -> Any:
    """Build the journaled bridge that owns every primary prompt lifecycle.

    The launched server's fenceable process identity is passed both as the
    dispatch process identity and as the isolated-transport ``server_identity``,
    so the pre-prompt egress gate can bind the transport target to the
    service-owned session server the service actually launched rather than to a
    bare loopback address.
    """
    return session_bridge_mod.JournaledSessionBridge(
        bridge,
        ledger,
        job_id=int(job_id),
        run_id=str(run_id),
        policy=policy,
        process_id=process_id,
        server_identity=process_id,
        change_id=change_id,
    )


def _supervisor_prompt_plan(
    repo: Path, policy: Mapping[str, Any] | None
) -> dict[str, Any]:
    """Return the supervisor-primary reserve/dispatch plan, or an empty plan.

    With a recorded policy the plan carries the reserved cost estimate and the
    pricing-catalog version (computed exactly as the dispatch boundary does,
    through the pricing catalog), plus the pinned supervisor model translated
    to the server's ``{providerID, modelID}`` shape. A policy whose supervisor
    pin cannot be priced blocks the session rather than prompting unbudgeted.
    Without a recorded policy there is nothing to reserve against, so the plan
    is empty and no reservation is written.
    """
    if not isinstance(policy, Mapping):
        return {}
    try:
        from lib.orchestrator import cost as cost_mod

        estimate, catalog_version = cost_mod.reservation_estimate_for_dispatch(
            Path(repo), policy, model_policy_mod.SUPERVISOR_ROLE
        )
        pinned = cost_mod.pinned_model_for_role(
            policy, model_policy_mod.SUPERVISOR_ROLE
        )
    except Exception as exc:  # noqa: BLE001 - every failure is a named blocker
        raise broker_mod.BrokerUnavailableError(
            "the supervisor-primary prompt cannot be budgeted before dispatch: "
            f"{type(exc).__name__}: {exc}; operator action required: qualify the "
            "supervisor role's pinned model in the pricing catalog, then record "
            "an updated policy revision"
        ) from exc
    return {
        "reserved_cost_usd": float(estimate),
        "reserved_elapsed_minutes": 0.0,
        "pricing_catalog_version": catalog_version,
        "model": session_bridge_mod.parse_model_identity(pinned) if pinned else None,
    }


def _apply_briefing(
    runtime: "PrimarySessionRuntime",
    *,
    model: Mapping[str, Any] | None,
    agent: str | None,
    max_attempts: int,
    enabled: bool,
) -> Any:
    """Dispatch the bounded full/rebrief text to the managed session.

    The briefing is a supervised primary prompt like any other: it is
    journaled (identity before the server request), reserved, dispatched with
    the session/process identity, and reconciled from polled usage.
    """
    if not enabled or runtime.journaled is None:
        return None
    return runtime.dispatch_prompt(
        runtime.briefing.render(),
        stage="briefing",
        model=model,
        agent=agent,
        max_attempts=max_attempts,
    )


def session_change_id(session: "ServiceSession") -> str | None:
    """Return the single registered change id for the job, or ``None``.

    A job's protected snapshot membership is the authority; when exactly one
    change is registered its gate state belongs in the briefing. Multiple or
    unreadable membership yields ``None`` (no gate line) rather than guessing.
    """
    try:
        snapshot = broker_mod.load_protected_snapshot(session.ledger, session.job_id)
        change_ids = broker_mod.snapshot_change_ids(snapshot)
    except Exception:  # noqa: BLE001 - absent snapshot is durable-state absence
        return None
    return change_ids[0] if len(change_ids) == 1 else None


@dataclass
class PrimarySessionRuntime:
    """The service-managed primary session: bridge, identity, and briefing."""

    bridge: Any
    session_id: str
    server_address: str
    adopted: bool
    briefing: Any
    linkage: Any
    server: Any = None
    journaled: Any = None
    prompt_plan: dict[str, Any] = None  # type: ignore[assignment]

    def __post_init__(self) -> None:
        if self.prompt_plan is None:
            self.prompt_plan = {}

    def dispatch_prompt(
        self,
        text: str,
        *,
        stage: str = "primary",
        round_num: int | None = None,
        model: Mapping[str, Any] | None = None,
        agent: str | None = None,
        max_attempts: int = budget_mod.BACKOFF_MAX_ATTEMPTS,
        sleep: Any = None,
    ) -> dict[str, Any]:
        """Dispatch one primary prompt through the journaled bridge.

        Every side effect goes through the journal lane: the intent carrying
        the request identity is committed before the server request, the
        supervisor-role reservation is committed first, the dispatch record
        binds the session and server process identity, and the observed usage
        is reconciled from the polled terminal result. A lost acknowledgement
        is recovered by lookup, never by re-prompting.
        """
        if self.journaled is None:
            raise broker_mod.BrokerUnavailableError(
                "the primary session has no journaled session bridge; refusing "
                "to prompt a supervised primary outside the action journal"
            )
        plan = self.prompt_plan or {}
        reserved_cost = plan.get("reserved_cost_usd")
        identity = self.journaled.prompt(
            self.session_id,
            text=text,
            stage=stage,
            round_num=round_num,
            role=model_policy_mod.SUPERVISOR_ROLE,
            model=model if model is not None else plan.get("model"),
            agent=agent,
            reserved_cost_usd=reserved_cost,
            reserved_elapsed_minutes=float(
                plan.get("reserved_elapsed_minutes") or 0.0
            ),
            pricing_catalog_version=plan.get("pricing_catalog_version"),
        )
        kwargs: dict[str, Any] = {"max_attempts": max_attempts}
        if sleep is not None:
            kwargs["sleep"] = sleep
        if not identity.acknowledged:
            recovery = self.journaled.recover_lost_ack(identity, **kwargs)
            return {
                "identity": identity,
                "result": recovery["result"],
                "reconciled": recovery["reconciled"],
                "terminal": recovery.get("terminal", recovery["reconciled"]),
                "duplicate_prompt_issued": recovery["duplicate_prompt_issued"],
            }
        result = session_bridge_mod.poll_until_terminal(
            self.bridge, self.session_id, marker=identity.request_id, **kwargs
        )
        outcome = self.journaled.resolve(identity, result=result)
        return {
            "identity": identity,
            "result": result,
            "outcome": outcome,
            "reconciled": outcome != "uncertain",
            "duplicate_prompt_issued": False,
        }

    def close(self) -> None:
        try:
            self.bridge.transport.close()
        except Exception:  # pragma: no cover - best-effort close
            pass
        if self.server is not None:
            self.server.terminate()

    def __enter__(self) -> "PrimarySessionRuntime":
        return self

    def __exit__(self, *exc: Any) -> None:
        self.close()


__all__ = [
    "ENV_SERVER_COMMAND",
    "ENV_STATE_FILE",
    "OPERATOR_SOCKET_ENV",
    "PrimarySessionRuntime",
    "Registration",
    "ServiceEndpointHost",
    "ServiceSession",
    "SessionServerProcess",
    "WORKER_SOCKET_ENV",
    "call_operator",
    "call_worker_actions",
    "execution_boundary_reason",
    "in_supervised_execution",
    "install_projection_writer",
    "is_registered",
    "launch_session_server",
    "ledger_path",
    "open_primary_session",
    "open_registration",
    "open_service_host",
    "open_service_session",
    "persist_projection",
    "project_broker_state",
    "require_supervised_authorization",
    "service_socket_path",
    "session_change_id",
    "session_server_command",
    "set_transport",
    "state_file_configured",
]
