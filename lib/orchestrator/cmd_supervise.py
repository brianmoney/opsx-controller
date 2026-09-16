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
import sys
from pathlib import Path

from lib.orchestrator import planref
from lib.orchestrator import supervision as supervision_mod
from lib.supervisor import authority
from lib.supervisor import broker as broker_mod


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
            repo, cfg, store_path=store_path, job_id=job_id
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
