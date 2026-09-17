"""``opsx-plan supervise`` namespace (status / probe / serve).

Owns the operator-facing surface of the authority boundary. ``status`` and
``probe`` are read-only diagnostics over :mod:`lib.supervisor.authority`; they
neither provision accounts, install units, nor write the authority store.
``serve`` is the trusted service-side endpoint host: it boots the supervised
broker session (opening the service-owned ledger and installing the projection
writer the broker requires) before accepting any operator or worker endpoint
request, so live receipt transactions always record and regenerate the JSON
projection.

- ``supervise status`` reports the capability of the host and always exits 0,
  because a report about an unsupported host is still a successful report.
- ``supervise probe`` runs the single fail-closed gate: on an unsupported or
  unprovisioned host it exits non-zero naming ``UnsupportedHostError``; on an
  available host it runs the mandatory activation probe and exits non-zero
  naming ``ActivationProbeError`` when the boundary does not hold.
- ``supervise serve`` boots that trusted session and dispatches authenticated
  endpoint requests; it fails closed with ``BrokerUnavailableError`` when the
  service store, the registered job, the principals, or the endpoint sockets
  are not provisioned.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import time
import uuid
from pathlib import Path
from typing import Any

from lib.models import resolver as model_resolver
from lib.orchestrator import base
from lib.orchestrator import planref
from lib.orchestrator import supervision as supervision_mod
from lib.supervisor import authority
from lib.supervisor import broker as broker_mod
from lib.supervisor import broker_client as broker_client_mod
from lib.supervisor import endpoints as endpoints_mod
from lib.supervisor import lifecycle as lifecycle_mod
from lib.supervisor import ledger as ledger_mod
from lib.supervisor import lock as lock_mod
from lib.supervisor import watchdog as watchdog_mod


def cmd_supervise_status(args: argparse.Namespace) -> int:
    """opsx-plan supervise status [--json] — report backend capability."""
    report = authority.detect_backend()

    if getattr(args, "json", False):
        print(json.dumps(report.as_dict(), indent=2, sort_keys=True))
        return 0

    print(f"authority backend: {report.backend}")
    print(f"capability: {report.status}")
    if report.principals is not None:
        for principal in report.principals.as_list():
            uid = principal.uid if principal.uid is not None else "(not provisioned)"
            print(f"  {principal.role:<8} {principal.name}  uid={uid}")
    if report.state_path is not None:
        print(f"authority store: {report.state_path}")
    for reason in report.reasons:
        print(f"  reason: {reason}")
    if report.status == authority.STATUS_UNPROVISIONED:
        print(f"  provision: {report.provisioning_pointer}")
    return 0


def cmd_supervise_probe(args: argparse.Namespace) -> int:
    """opsx-plan supervise probe — run the fail-closed enablement gate."""
    try:
        report = authority.require_authority_backend()
    except authority.UnsupportedHostError as exc:
        print(f"error: UnsupportedHostError: {exc}", file=sys.stderr)
        print(f"  provision: {authority.PROVISIONING_POINTER}", file=sys.stderr)
        return 1
    except authority.ActivationProbeError as exc:
        print(f"error: ActivationProbeError: {exc}", file=sys.stderr)
        return 1

    print(
        "supervision gate passed: the activation probe confirmed the worker "
        f"domain is denied write access to the authority store "
        f"({report.state_path})"
    )
    return 0


def cmd_supervise_serve(args: argparse.Namespace) -> int:
    """opsx-plan supervise serve — host the trusted broker endpoint surface.

    Boots the service session (which installs and retains the trusted
    projection writer) and the operator/worker endpoint sockets before
    accepting any request, then dispatches authenticated requests until
    stopped. A provisioning failure — missing service store, unregistered
    worktree, unresolvable allowed principal, or a socket that cannot be
    bound — fails closed with the named broker error rather than serving
    unauthenticated or unprojected requests.
    """
    repo = Path(args.repo).resolve()
    try:
        plan_src = planref.resolve_plan(repo, getattr(args, "plan", None))
        cfg = planref.load_plan(
            planref._resolve_plan_path(repo, plan_src), repo=repo
        )
        store_path = (
            Path(args.store).resolve() if getattr(args, "store", None) else None
        )
        job_id = getattr(args, "job_id", None)
        host = supervision_mod.open_service_host(
            repo, cfg, store_path=store_path, job_id=job_id,
            watchdog_redrive=_watchdog_redrive(repo, getattr(args, "plan", None)),
        )
    except broker_mod.BrokerError as exc:
        print(f"error: {type(exc).__name__}: {exc}", file=sys.stderr)
        return 1
    except Exception as exc:  # noqa: BLE001 - provisioning failure, fail closed
        print(f"error: BrokerUnavailableError: {exc}", file=sys.stderr)
        return 1

    try:
        host.bind()
    except broker_mod.BrokerError as exc:
        # An unbindable endpoint is a named fail-closed provisioning error: the
        # host has already torn the session down, and the CLI reports
        # BrokerUnavailableError instead of letting a raw OSError escape.
        print(f"error: {type(exc).__name__}: {exc}", file=sys.stderr)
        host.close()
        return 1
    except Exception as exc:  # noqa: BLE001 - provisioning failure, fail closed
        print(f"error: BrokerUnavailableError: {exc}", file=sys.stderr)
        host.close()
        return 1

    try:
        print(
            f"supervise serve: session for job {host.session.job_id} is live; "
            f"operator={host.operator_socket} worker={host.worker_socket}"
        )
        if getattr(args, "primary_session", False):
            runtime = host.start_primary_session()
            print(
                f"supervise serve: primary session {runtime.session_id} "
                f"{'adopted' if runtime.adopted else 'started'} at "
                f"{runtime.server_address} (briefing: {runtime.briefing.mode})"
            )
        if getattr(args, "once", False):
            host.poll(timeout=float(getattr(args, "timeout", 30.0)))
        else:
            host.serve_forever(timeout=1.0)
    except KeyboardInterrupt:  # pragma: no cover - interactive shutdown
        pass
    except broker_mod.BrokerError as exc:
        # A primary session that cannot start or adopt (no restricted-spawn
        # mechanism, no address, an unsupported server) is a named fail-closed
        # provisioning error, never a raw OSError out of the serve loop.
        print(f"error: {type(exc).__name__}: {exc}", file=sys.stderr)
        host.close()
        return 1
    finally:
        host.close()
    return 0


# ---------------------------------------------------------------------------
# Lifecycle: register / start / inspect / resume / pause / drain / cancel
# ---------------------------------------------------------------------------

_LIFECYCLE_ERRORS = (
    lifecycle_mod.LifecycleError,
    broker_mod.BrokerError,
    authority.AuthorityError,
    ledger_mod.LedgerError,
)


def _fail(exc: BaseException) -> int:
    print(f"error: {type(exc).__name__}: {exc}", file=sys.stderr)
    return 1


def _store_path(repo: Path, store: str | None) -> Path:
    if store:
        return Path(store).resolve()
    path = supervision_mod.ledger_path(repo)
    if path is None:
        raise broker_mod.BrokerUnavailableError(
            "no service-owned supervision store is configured; the supervised "
            "lifecycle cannot reach its durable state"
        )
    return path


def _open_ledger(repo: Path, store: str | None, *, create: bool) -> Any:
    path = _store_path(repo, store)
    if not create and not path.exists():
        raise lifecycle_mod.UnknownJobError(
            f"the supervision store {path} does not exist; no supervised job "
            "is registered for this worktree"
        )
    return ledger_mod.open_ledger(path, repository_root=repo, create=create)


def _resolve_job(ledger: Any, repo: Path, args: argparse.Namespace) -> Any:
    job_id = getattr(args, "job_id", None)
    if job_id is not None:
        return lifecycle_mod.require_job(ledger, int(job_id))
    return lifecycle_mod.job_for_worktree(ledger, repo, repository_root=repo)


def _authority_config(report: Any) -> dict[str, Any]:
    """Return the standing permissions recorded as the authority configuration."""
    config: dict[str, Any] = {
        "mode": "policy-bound",
        "backend": getattr(report, "backend", None),
        "status": getattr(report, "status", None),
    }
    principals = getattr(report, "principals", None)
    if principals is not None:
        for principal in principals.as_list():
            config[f"{principal.role}_principal"] = {
                "name": principal.name,
                "uid": principal.uid,
            }
    return config


def _role_models(cfg: dict) -> dict[str, str]:
    models = cfg.get("models") or {}
    resolved: dict[str, str] = {}
    for role, entry in models.items():
        model = getattr(entry, "model", None)
        if isinstance(entry, str):
            model = entry
        if model:
            resolved[str(role)] = str(model)
    return resolved


def _operator_socket_configured() -> bool:
    return (
        broker_client_mod.endpoint_socket_path(endpoints_mod.ENDPOINT_OPERATOR)
        is not None
    )


def _mediated_or_direct(
    repo: Path,
    job_id: int,
    verb: str,
    *,
    store: str | None,
    payload: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Run *verb* through the operator endpoint when it is configured.

    A live job's mutation is mediated over the operator OS-authenticated path;
    when no operator socket is configured the command runs as the trust root
    directly against the service-owned ledger (the non-live job path). A
    configured-but-unreachable endpoint fails closed with the named
    broker-unavailable error rather than acting unmediated.
    """
    if _operator_socket_configured():
        return supervision_mod.call_operator(job_id, verb, **(payload or {}))
    ledger = _open_ledger(repo, store, create=False)
    try:
        job = _LIFECYCLE_VERBS[verb](ledger, job_id)
        outcome: dict[str, Any] = {
            "verb": verb,
            "job_id": int(job_id),
            "state": str(job["state"]),
        }
        request = ledger.latest_steering_request(int(job_id))
        if request is not None and request["request_id"]:
            outcome["request_id"] = str(request["request_id"])
            outcome["ack_state"] = request["ack_state"]
            outcome["ack_boundary"] = request["ack_boundary"]
    finally:
        ledger.close()
    return outcome


_LIFECYCLE_VERBS: dict[str, Any] = {
    "pause": lifecycle_mod.pause,
    "drain": lifecycle_mod.drain,
    "cancel": lifecycle_mod.cancel,
}


def cmd_supervise_register(args: argparse.Namespace) -> int:
    """opsx-plan supervise register — record the supervised job."""
    repo = Path(args.repo).resolve()
    try:
        plan_src = planref.resolve_plan(repo, getattr(args, "plan", None))
        plan_abs = planref._resolve_plan_path(repo, plan_src)
        cfg = planref.load_plan(plan_abs, repo=repo)
        manifest_content = plan_abs.read_text(encoding="utf-8")
        # Detection only for the recorded standing permissions; the fail-closed
        # backend gate (probe included) runs once, inside lifecycle.register.
        report = authority.detect_backend()
        allowlist = model_resolver.resolve_allowlist(repo)
        policy = lifecycle_mod.assemble_registration_policy(
            authority_config=_authority_config(report),
            role_models=_role_models(cfg),
            allowlist_models=allowlist.models,
            allowlist_source=allowlist.source,
            total_cost_usd=float(getattr(args, "budget_usd", 0.0) or 0.0),
            per_action_cost_usd=getattr(args, "per_action_usd", None),
            total_elapsed_minutes=getattr(args, "budget_minutes", None),
            per_action_elapsed_minutes=getattr(args, "per_action_minutes", None),
            execution_deadline_minutes=getattr(args, "deadline_minutes", None),
            max_incident_attempts=getattr(args, "max_incident_attempts", None),
        )
        linkage = {
            "adapter": cfg["adapter"],
            "state_file": cfg["state_file"],
            "start_primary_session": bool(
                getattr(args, "primary_session", False)
            ),
        }
        ledger = _open_ledger(repo, getattr(args, "store", None), create=True)
        try:
            job_id = lifecycle_mod.register(
                ledger,
                worktree=repo,
                repository_root=repo,
                owner=f"operator:{os.getuid()}",
                operator=str(os.getuid()),
                policy=policy,
                manifest_content=manifest_content,
                run_id=f"supervised-{cfg['name']}-{uuid.uuid4().hex[:12]}",
                linkage_config=linkage,
            )
            job = ledger.get_job(job_id)
        finally:
            ledger.close()
    except _LIFECYCLE_ERRORS as exc:
        return _fail(exc)
    if getattr(args, "json", False):
        print(json.dumps(
            {"job_id": job_id, "state": str(job["state"]), "plan": cfg["name"]},
            indent=2, sort_keys=True,
        ))
    else:
        print(f"supervise register: job {job_id} recorded ({job['state']})")
    return 0


def _drive_supervised_run(repo: Path, plan_src: str) -> int:
    """Drive the existing run engine as the supervised execution.

    The engine is the existing ``cmd_run`` path — no new DAG — and the caller
    has already recorded the live service-owned execution fence, so every inner
    dispatch passes the journal's mediated boundary.
    """
    module = sys.modules.get("opsx_plan")
    if module is None or not hasattr(module, "cmd_run"):
        raise broker_mod.BrokerUnavailableError(
            "the supervised run engine is unavailable in this process; invoke "
            "`opsx-plan supervise start` through the opsx-plan entrypoint"
        )
    namespace = argparse.Namespace(repo=str(repo), plan=plan_src)
    return int(module.cmd_run(namespace))


def _activate_and_drive(args: argparse.Namespace, verb: str) -> int:
    repo = Path(args.repo).resolve()
    try:
        plan_src = planref.resolve_plan(repo, getattr(args, "plan", None))
        cfg = planref.load_plan(planref._resolve_plan_path(repo, plan_src), repo=repo)
        ledger = _open_ledger(repo, getattr(args, "store", None), create=False)
        try:
            job = _resolve_job(ledger, repo, args)
            job_id = int(job["id"])
            transition = lifecycle_mod.resume if verb == "resume" else lifecycle_mod.start
            job = transition(ledger, job_id)
            identity = lock_mod.current_identity()
            ledger.record_fencing(
                job_id,
                event="acquired",
                owner=f"opsx-plan supervise {verb}",
                pid=int(identity["pid"]),
                process_start=identity["process_start"],
                boot_id=identity["boot_id"],
                host=identity["host"],
            )
        finally:
            ledger.close()
    except _LIFECYCLE_ERRORS as exc:
        return _fail(exc)
    try:
        if getattr(args, "no_drive", False):
            print(
                f"supervise {verb}: job {job_id} is {job['state']}; "
                "engine drive skipped (--no-drive)"
            )
            return 0
        print(f"supervise {verb}: job {job_id} is {job['state']}; driving the run engine")
        return _drive_supervised_run(repo, plan_src)
    except _LIFECYCLE_ERRORS as exc:
        return _fail(exc)
    finally:
        _release_fence(repo, job_id, getattr(args, "store", None))


def _release_fence(repo: Path, job_id: int, store: str | None) -> None:
    try:
        ledger = _open_ledger(repo, store, create=False)
    except Exception:
        return
    try:
        identity = lock_mod.current_identity()
        ledger.record_fencing(
            int(job_id),
            event="released",
            owner="opsx-plan supervise",
            pid=int(identity["pid"]),
            process_start=identity["process_start"],
            boot_id=identity["boot_id"],
            host=identity["host"],
        )
    except Exception:
        pass
    finally:
        ledger.close()


def cmd_supervise_start(args: argparse.Namespace) -> int:
    """opsx-plan supervise start — activate and drive a registered job."""
    return _activate_and_drive(args, "start")


def cmd_supervise_resume(args: argparse.Namespace) -> int:
    """opsx-plan supervise resume — revalidate and reactivate a paused job."""
    return _activate_and_drive(args, "resume")


def _lifecycle_mutation(args: argparse.Namespace, verb: str) -> int:
    repo = Path(args.repo).resolve()
    try:
        ledger = _open_ledger(repo, getattr(args, "store", None), create=False)
        try:
            job = _resolve_job(ledger, repo, args)
            job_id = int(job["id"])
        finally:
            ledger.close()
        outcome = _mediated_or_direct(
            repo,
            job_id,
            verb,
            store=getattr(args, "store", None),
        )
    except _LIFECYCLE_ERRORS as exc:
        return _fail(exc)
    if getattr(args, "json", False):
        print(json.dumps(outcome, indent=2, sort_keys=True))
    else:
        print(
            f"supervise {verb}: job {job_id} is {outcome.get('state', 'unknown')}"
        )
        if outcome.get("request_id"):
            ack = outcome.get("ack_state") or "pending"
            boundary = outcome.get("ack_boundary")
            suffix = f" at {boundary}" if boundary else ""
            print(f"  request: {outcome['request_id']} ({ack}{suffix})")
    return 0


def cmd_supervise_pause(args: argparse.Namespace) -> int:
    """opsx-plan supervise pause — the pause stop boundary."""
    return _lifecycle_mutation(args, "pause")


def cmd_supervise_drain(args: argparse.Namespace) -> int:
    """opsx-plan supervise drain — the drain stop boundary."""
    return _lifecycle_mutation(args, "drain")


def cmd_supervise_cancel(args: argparse.Namespace) -> int:
    """opsx-plan supervise cancel — the terminal cancellation."""
    return _lifecycle_mutation(args, "cancel")


def cmd_supervise_inspect(args: argparse.Namespace) -> int:
    """opsx-plan supervise inspect — a read-only job projection.

    Requires neither the execution lock nor a live service: it opens the
    service-owned ledger read-only and projects the job's state, recorded
    waits, policy revision, budget posture, recent actions, incidents, and the
    pending ``(manual)`` operator checklist.
    """
    repo = Path(args.repo).resolve()
    try:
        ledger = _open_ledger(repo, getattr(args, "store", None), create=False)
        try:
            job = _resolve_job(ledger, repo, args)
            job_id = int(job["id"])
            projection = _inspect_projection(ledger, repo, job)
        finally:
            ledger.close()
    except _LIFECYCLE_ERRORS as exc:
        return _fail(exc)
    if getattr(args, "json", False):
        print(json.dumps(projection, indent=2, sort_keys=True))
    else:
        _print_inspection(projection)
    return 0


def _inspect_projection(ledger: Any, repo: Path, job: Any) -> dict[str, Any]:
    """Return the shared read-only supervision projection for *job*.

    Delegates to :func:`lib.orchestrator.supervision.project_job` so
    ``supervise inspect``, ``status``, ``report``, and the dashboard all build
    one field model. Every existing ``inspect`` key keeps its name; the
    projection is a superset.
    """
    return supervision_mod.project_job(ledger, job, repo=repo)


def _print_inspection(projection: dict[str, Any]) -> None:
    print(f"job {projection['job_id']}: {projection['state']}")
    print(f"  worktree: {projection['worktree']}")
    print(f"  owner: {projection['owner']}")
    print(f"  policy revision: {projection['policy']['revision']}")
    budgets = projection["budget_posture"]["budgets"]
    consumption = projection["budget_posture"]["consumption"]
    print(
        "  budget: total_cost_usd="
        f"{budgets.get('total_cost_usd')} charged_cost_usd="
        f"{consumption.get('cost_usd')} elapsed_minutes="
        f"{consumption.get('elapsed_minutes')}"
    )
    if projection["waits"]:
        print("  waits:")
        for wait in projection["waits"]:
            print(
                f"    [{wait['state']}] {wait['kind']} {wait['checkpoint']} "
                f"({wait['started_at']})"
            )
    if projection["recent_actions"]:
        print("  recent actions:")
        for action in projection["recent_actions"]:
            print(f"    {action['id']} {action['kind']} {action['state']}")
    if projection["recent_incidents"]:
        print("  recent incidents:")
        for incident in projection["recent_incidents"]:
            print(f"    {incident['id']} {incident['kind']}: {incident['summary']}")
    if projection["pending_manual_tasks"]:
        print("  pending manual tasks (operator checklist):")
        for cid, tasks in projection["pending_manual_tasks"].items():
            for task in tasks:
                print(f"    {cid}: {task}")


# ---------------------------------------------------------------------------
# Watchdog: deterministic classification and bounded reconstitution
# ---------------------------------------------------------------------------

_WATCHDOG_ERRORS = _LIFECYCLE_ERRORS + (watchdog_mod.WatchdogError,)


def _watchdog_redrive(repo: Path, plan: Any) -> Any:
    """Return the production re-drive adapter for the watchdog.

    The adapter reuses the existing supervised run engine (no new DAG); it is
    the same engine ``supervise start``/``resume`` drive. When no plan can be
    resolved the adapter refuses with the named watchdog error instead of
    guessing, and the runner records that as blocking state.
    """
    try:
        plan_src = planref.resolve_plan(repo, plan)
    except Exception:  # noqa: BLE001 - a missing plan is a refusal, not a crash
        plan_src = None

    def redrive(ledger: Any, job_id: int) -> Any:
        if plan_src is None:
            raise watchdog_mod.WatchdogRefused(
                "no resolvable plan is available to reconstitute the job"
            )
        return _drive_supervised_run(repo, plan_src)

    return redrive


def cmd_supervise_watchdog(args: argparse.Namespace) -> int:
    """opsx-plan supervise watchdog — the deterministic watchdog tick.

    Every tick drives :meth:`Watchdog.tick` over all registered non-terminal
    jobs, so the command evaluates and reports each one; an explicit
    ``--job-id`` only selects which job's report is shown at the top level and
    never narrows the tick. It runs exactly one tick with ``--once`` or the
    deterministic loop otherwise, reporting each job's classification, its
    three separate signals, its restart-attempt state, and its recent
    reconstitution events in human or ``--json`` form. It requires neither the
    worktree execution lock nor a live service and fails closed with the named
    unknown-job error when no registered supervised job exists.
    """
    repo = Path(args.repo).resolve()
    try:
        ledger = _open_ledger(repo, getattr(args, "store", None), create=False)
        try:
            jobs = list(ledger.list_jobs())
            requested = getattr(args, "job_id", None)
            if not jobs and requested is None:
                raise lifecycle_mod.UnknownJobError(
                    "no supervised job is registered for this worktree; the "
                    "watchdog command fails closed"
                )
            non_terminal = [
                job
                for job in jobs
                if str(job["state"]) not in ledger_mod.TERMINAL_JOB_STATES
            ]
            if requested is not None:
                primary = lifecycle_mod.require_job(ledger, int(requested))
            elif non_terminal:
                primary = non_terminal[0]
            else:
                primary = jobs[0]

            runner = watchdog_mod.Watchdog(
                ledger,
                repo=repo,
                redrive=_watchdog_redrive(repo, getattr(args, "plan", None)),
            )
            if getattr(args, "once", False):
                assessments = runner.tick()
            else:
                interval = float(getattr(args, "interval", 5.0) or 5.0)
                assessments = runner.tick()
                try:
                    while True:
                        time.sleep(interval)
                        assessments = runner.tick()
                except KeyboardInterrupt:  # pragma: no cover - interactive stop
                    pass

            reports = [_watchdog_job_report(runner, job) for job in non_terminal]
            if int(primary["id"]) not in {item["job_id"] for item in reports}:
                reports.append(_watchdog_job_report(runner, primary))
        finally:
            ledger.close()
    except _WATCHDOG_ERRORS as exc:
        return _fail(exc)

    primary_id = int(primary["id"])
    payload = dict(
        next(item for item in reports if item["job_id"] == primary_id)
    )
    payload["jobs"] = reports
    payload["assessments"] = [item.as_dict() for item in assessments]
    if getattr(args, "json", False):
        print(json.dumps(payload, indent=2, sort_keys=True))
    else:
        _print_watchdog(payload)
    return 0


def _watchdog_job_report(runner: Any, job: Any) -> dict[str, Any]:
    """Build one job's read-only watchdog report with its job id attached."""
    report = runner.report(job)
    report["job_id"] = int(job["id"])
    return report


def _print_watchdog(payload: dict[str, Any]) -> None:
    for report in payload.get("jobs") or [payload]:
        _print_watchdog_job(report)


def _print_watchdog_job(report: dict[str, Any]) -> None:
    print(
        f"supervise watchdog: job {report['job_id']} "
        f"classification={report['classification']} action={report['action']}"
    )
    signals = report["signals"]
    print(
        "  signals: liveness="
        f"{signals['liveness']} progress={signals['progress']} "
        f"deadline={signals['deadline']}"
    )
    restart = report["restart"]
    print(
        f"  restart: attempts={restart['attempts']} limit={restart['limit']} "
        f"next_allowed_at={restart['next_allowed_at']}"
    )
    if report.get("reason"):
        print(f"  reason: {report['reason']}")
    if report.get("blocked"):
        print("  blocked: an operator action is required")
    events = report.get("recent_events") or []
    if events:
        print("  recent events:")
        for event in events:
            print(
                f"    [{event['kind']}] {event['classification'] or '-'} "
                f"{event['reason']} ({event['created_at']})"
            )
