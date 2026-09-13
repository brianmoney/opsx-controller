"""``opsx-plan supervise`` namespace (status / probe).

Owns the operator-facing surface of the authority boundary. Both subcommands
are read-only diagnostics over :mod:`lib.supervisor.authority`; neither
provisions accounts, installs units, or writes the authority store.

- ``supervise status`` reports the capability of the host and always exits 0,
  because a report about an unsupported host is still a successful report.
- ``supervise probe`` runs the single fail-closed gate: on an unsupported or
  unprovisioned host it exits non-zero naming ``UnsupportedHostError``; on an
  available host it runs the mandatory activation probe and exits non-zero
  naming ``ActivationProbeError`` when the boundary does not hold.
"""

from __future__ import annotations

import argparse
import json
import sys

from lib.supervisor import authority


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
