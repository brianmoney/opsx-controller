"""Tracked service tool: the primary session's only side-effecting surface.

The supervised primary is an LLM session whose capability is deliberately
bounded to reading journaled evidence and invoking one tracked tool. This
module is the verb dispatcher behind that tool: it maps a small, closed set of
verbs one-to-one onto the **worker-actions** endpoint handlers and carries them
over :mod:`lib.supervisor.broker_client`.

Design rules enforced here:

- Standard library only, and it imports no other runtime package.
- The **worker endpoint only**. :func:`resolve_endpoint` refuses anything but
  the scoped worker-actions endpoint, so this surface can never reach the
  operator endpoint (`approve`, `reset_change`, `revise_policy`, `enable`,
  `cancel` are not dispatchable here).
- Journaling is structural, not voluntary: every verb maps to an endpoint
  handler that writes the ledger before its side effect, so a service-tool
  invocation is a journaled supervised action rather than an out-of-band write.
- Fails closed: an unknown verb, a missing verb, or a missing job id raises the
  named :class:`ServiceToolError` before any request is sent.

Importing this module has no side effects, parses no arguments, spawns no
process, and touches no ``.opsx-plan/`` state.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from typing import Any, Callable, Mapping, Sequence

from lib.supervisor import agent_contracts as agent_contracts_mod
from lib.supervisor import broker as broker_mod
from lib.supervisor import broker_client as broker_client_mod
from lib.supervisor import endpoints as endpoints_mod
from lib.supervisor import model_policy as model_policy_mod

# The worker-actions verbs the primary may invoke, in the endpoint's own
# vocabulary. This is the whole surface: an operator verb is never reachable.
SERVICE_VERBS: tuple[str, ...] = (
    "report_status",
    "request_action",
    "record_evidence",
    "heartbeat",
    "release_delegated_gate",
    "report_violation",
    "choose_remedy",
)

# Environment the shim reads. The socket env names are the broker client's, so
# the worker endpoint is resolved from the worker domain's own configuration
# and an operator socket is never consulted. The role and service-identity
# fields are *required*: a supervised request without them is a
# ``policy_violation`` at the endpoint, never the legacy path.
JOB_ID_ENV = "OPSX_SUPERVISOR_JOB_ID"
SERVICE_IDENTITY_ENV = "OPSX_SUPERVISOR_SERVICE_PRINCIPAL"
ROLE_ENV = "OPSX_SUPERVISOR_ROLE"
AGENT_ENV = "OPSX_SUPERVISOR_AGENT"


class ServiceToolError(Exception):
    """A service-tool invocation was malformed or named an unexposed verb."""


def allowed_verbs() -> tuple[str, ...]:
    """Return the closed verb surface, in a stable order."""
    return SERVICE_VERBS


def resolve_endpoint(kind: str | None = None) -> str:
    """Return the endpoint this tool may dispatch to, refusing anything else.

    The tracked service tool exists to reach the scoped worker-actions
    endpoint. Passing any other endpoint — in particular the operator
    endpoint — is refused with the named error rather than dialed, so the
    primary's surface cannot be widened from inside the worker domain.
    """
    requested = endpoints_mod.ENDPOINT_WORKER if kind is None else str(kind)
    if requested != endpoints_mod.ENDPOINT_WORKER:
        raise ServiceToolError(
            f"the tracked service tool dispatches only to the worker-actions "
            f"endpoint; refusing endpoint {requested!r}"
        )
    return endpoints_mod.ENDPOINT_WORKER


def build_request(
    verb: str,
    *,
    job_id: int,
    role: str | None = None,
    observed_agent: str | None = None,
    service_identity: str | None = None,
    requested_permissions: Sequence[str] | None = None,
    payload: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    """Build the endpoint request for *verb*, failing closed on a bad one.

    An unknown verb, a non-integer job id, or an operator-only verb is refused
    before any request is framed. The supervised identity fields (role,
    observed agent, service identity) are **mandatory**: a request without them
    would arrive at the worker endpoint unidentified and be refused there, so
    this tool refuses to frame it at all. The caller-supplied payload may not
    override the identity fields.
    """
    if not isinstance(verb, str) or verb not in SERVICE_VERBS:
        raise ServiceToolError(
            f"verb {verb!r} is not exposed by the tracked service tool; "
            f"expected one of {', '.join(SERVICE_VERBS)}"
        )
    try:
        job_id = int(job_id)
    except (TypeError, ValueError) as exc:
        raise ServiceToolError(
            "a service-tool invocation requires an integer job id"
        ) from exc
    role = role.strip() if isinstance(role, str) and role.strip() else None
    observed_agent = (
        observed_agent.strip()
        if isinstance(observed_agent, str) and observed_agent.strip()
        else None
    )
    service_identity = (
        service_identity.strip()
        if isinstance(service_identity, str) and service_identity.strip()
        else None
    )
    if role is None:
        raise ServiceToolError(
            f"the tracked service tool requires the supervised role "
            f"(pass --role or set {ROLE_ENV}); an unidentified request is refused"
        )
    expected_agent = agent_contracts_mod.role_agent(role)
    if expected_agent is None:
        raise ServiceToolError(
            f"role {role!r} has no registered supervised agent contract; the "
            "service tool refuses to frame an unregistered role"
        )
    if observed_agent is None:
        raise ServiceToolError(
            f"the tracked service tool requires the observed agent "
            f"(pass --agent or set {AGENT_ENV}); the role's concrete agent is "
            f"{expected_agent!r}"
        )
    if service_identity is None:
        raise ServiceToolError(
            f"the tracked service tool requires the job's registered service "
            f"identity (pass --service-identity or set {SERVICE_IDENTITY_ENV}); "
            "an unidentified request is refused"
        )
    request: dict[str, Any] = {
        "verb": verb,
        "job_id": job_id,
        agent_contracts_mod.WORKER_ROLE_FIELD: role,
        agent_contracts_mod.WORKER_AGENT_FIELD: observed_agent,
        agent_contracts_mod.WORKER_IDENTITY_FIELD: service_identity,
    }
    for key, value in dict(payload or {}).items():
        name = str(key)
        if name in request or name in (
            agent_contracts_mod.WORKER_ROLE_FIELD,
            agent_contracts_mod.WORKER_AGENT_FIELD,
            agent_contracts_mod.WORKER_IDENTITY_FIELD,
            "verb",
            "job_id",
        ):
            raise ServiceToolError(
                f"payload key {name!r} would override a required supervised "
                "identity field; refusing to frame the request"
            )
        request[name] = value
    if requested_permissions is not None:
        request["requested_permissions"] = list(requested_permissions)
    return request


def parse_payload(tokens: Sequence[str]) -> dict[str, Any]:
    """Parse ``--key value`` / ``--key=value`` pairs into a payload mapping.

    A bare flag becomes ``True``; a repeated key's last value wins. JSON
    object/array values are decoded so structured evidence survives the
    command line intact.
    """
    payload: dict[str, Any] = {}
    index = 0
    tokens = list(tokens)
    while index < len(tokens):
        token = tokens[index]
        index += 1
        if not token.startswith("--"):
            raise ServiceToolError(
                f"unexpected argument {token!r}; payload arguments use --key value"
            )
        body = token[2:]
        if not body:
            raise ServiceToolError("an empty '--' argument is not a payload key")
        if "=" in body:
            key, _, raw = body.partition("=")
            payload[key.replace("-", "_")] = _coerce(raw)
            continue
        key = body.replace("-", "_")
        if index < len(tokens) and not tokens[index].startswith("--"):
            payload[key] = _coerce(tokens[index])
            index += 1
        else:
            payload[key] = True
    return payload


def _coerce(raw: str) -> Any:
    text = raw.strip()
    if text[:1] in ("{", "["):
        try:
            return json.loads(text)
        except (TypeError, ValueError):
            return raw
    return raw


def dispatch(
    verb: str,
    *,
    job_id: int,
    payload: Mapping[str, Any] | None = None,
    role: str | None = None,
    observed_agent: str | None = None,
    service_identity: str | None = None,
    requested_permissions: Sequence[str] | None = None,
    connector: Any = None,
    socket_path: os.PathLike[str] | str | None = None,
    env: Mapping[str, str] | None = None,
    timeout: float = broker_client_mod.DEFAULT_TIMEOUT_SECONDS,
) -> dict[str, Any]:
    """Dispatch one service-tool *verb* over the worker-actions endpoint.

    Fails closed through :class:`ServiceToolError` for a malformed invocation
    (including a request missing its supervised identity) and through the
    broker's named errors when the worker endpoint cannot be reached or
    refuses the request.
    """
    environment = os.environ if env is None else env
    if role is None:
        role = _env_value(ROLE_ENV, environment)
    if observed_agent is None:
        observed_agent = _env_value(AGENT_ENV, environment)
    if service_identity is None:
        service_identity = _env_value(SERVICE_IDENTITY_ENV, environment)
    request = build_request(
        verb,
        job_id=job_id,
        role=role,
        observed_agent=observed_agent,
        service_identity=service_identity,
        requested_permissions=requested_permissions,
        payload=payload,
    )
    kind = resolve_endpoint(None)
    try:
        return broker_client_mod.call(
            request,
            kind=kind,
            connector=connector,
            socket_path=socket_path,
            env=env,
            timeout=timeout,
        )
    except broker_mod.BrokerError:
        raise
    except Exception as exc:  # noqa: BLE001 - a transport failure is named
        raise ServiceToolError(
            f"the service-tool invocation {verb!r} failed: {exc}"
        ) from exc


def _env_value(name: str, env: Mapping[str, str] | None) -> str | None:
    environment = os.environ if env is None else env
    configured = environment.get(name)
    return configured.strip() if isinstance(configured, str) and configured.strip() else None


def _job_id_from(
    explicit: int | None, env: Mapping[str, str] | None
) -> int:
    if explicit is not None:
        try:
            return int(explicit)
        except (TypeError, ValueError) as exc:
            raise ServiceToolError("--job-id must be an integer") from exc
    environment = os.environ if env is None else env
    configured = (environment.get(JOB_ID_ENV) or "").strip()
    if not configured:
        raise ServiceToolError(
            f"no job id was supplied; pass --job-id or set {JOB_ID_ENV}"
        )
    try:
        return int(configured)
    except (TypeError, ValueError) as exc:
        raise ServiceToolError(
            f"{JOB_ID_ENV} is not an integer: {configured!r}"
        ) from exc


def _identity_from(
    explicit: str | None, env: Mapping[str, str] | None
) -> str | None:
    if explicit:
        return explicit
    return _env_value(SERVICE_IDENTITY_ENV, env)


def _role_from(explicit: Any, env: Mapping[str, str] | None) -> str | None:
    if isinstance(explicit, str) and explicit.strip():
        return explicit.strip()
    return _env_value(ROLE_ENV, env)


def _agent_from(explicit: Any, env: Mapping[str, str] | None) -> str | None:
    if isinstance(explicit, str) and explicit.strip():
        return explicit.strip()
    configured = _env_value(AGENT_ENV, env)
    return configured


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="opsx-supervise",
        description=(
            "Tracked supervised service tool. Dispatches a closed set of "
            "worker-actions verbs over the scoped worker endpoint; an operator "
            "verb is never reachable here. Every invocation carries the "
            "supervised role, observed agent, and registered service identity."
        ),
    )
    parser.add_argument("verb", nargs="?", help="one of: " + ", ".join(SERVICE_VERBS))
    parser.add_argument(
        "--job-id", dest="job_id", type=int, default=None,
        help=f"supervised job id (defaults to {JOB_ID_ENV})",
    )
    parser.add_argument(
        "--role", dest="role", default=None,
        help=f"supervised role (defaults to {ROLE_ENV})",
    )
    parser.add_argument(
        "--agent", dest="agent", default=None,
        help=f"observed concrete agent (defaults to {AGENT_ENV})",
    )
    parser.add_argument(
        "--service-identity", dest="service_identity", default=None,
        help=f"registered service identity (defaults to {SERVICE_IDENTITY_ENV})",
    )
    parser.add_argument(
        "--socket", dest="socket_path", default=None,
        help="explicit worker-actions socket path (testing seam)",
    )
    return parser


def list_verbs(argv: Sequence[str] | None = None) -> int:
    for verb in SERVICE_VERBS:
        print(verb)
    return 0


def main(
    argv: Sequence[str] | None = None,
    *,
    env: Mapping[str, str] | None = None,
    dispatch_fn: Callable[..., dict[str, Any]] | None = None,
) -> int:
    """CLI entrypoint for the ``opsx-supervise`` shim."""
    tokens = list(sys.argv[1:] if argv is None else argv)
    if not tokens or tokens[0] in ("-h", "--help"):
        build_parser().print_help()
        return 0 if tokens else 2
    if tokens[0] == "list-verbs":
        return list_verbs()
    try:
        payload = parse_payload(tokens[1:])
        job_id_arg = payload.pop("job_id", None)
        identity_arg = payload.pop("service_identity", None)
        role_arg = payload.pop("role", None)
        agent_arg = payload.pop("agent", None)
        socket_arg = payload.pop("socket", None)
        permissions_arg = payload.pop("requested_permissions", None)
        verb = tokens[0]
        job_id = _job_id_from(job_id_arg, env)
        identity = _identity_from(
            identity_arg if identity_arg is not True else None, env
        )
        role = _role_from(role_arg if role_arg is not True else None, env)
        agent = _agent_from(agent_arg if agent_arg is not True else None, env)
        if isinstance(permissions_arg, str):
            permissions: Sequence[str] | None = [
                item for item in permissions_arg.split(",") if item.strip()
            ]
        elif isinstance(permissions_arg, (list, tuple)):
            permissions = [str(item) for item in permissions_arg]
        else:
            permissions = None
        runner = dispatch_fn or dispatch
        result = runner(
            verb,
            job_id=job_id,
            payload=payload,
            role=role,
            observed_agent=agent,
            service_identity=identity,
            requested_permissions=permissions,
            socket_path=socket_arg if socket_arg is not True else None,
            env=env,
        )
    except ServiceToolError as exc:
        print(json.dumps({"ok": False, "error": "ServiceToolError", "message": str(exc)}))
        return 2
    except broker_mod.BrokerError as exc:
        print(
            json.dumps(
                {"ok": False, "error": type(exc).__name__, "message": str(exc)}
            )
        )
        return 3
    print(json.dumps({"ok": True, "result": result}, sort_keys=True, default=str))
    return 0


__all__ = [
    "AGENT_ENV",
    "JOB_ID_ENV",
    "ROLE_ENV",
    "SERVICE_IDENTITY_ENV",
    "SERVICE_VERBS",
    "ServiceToolError",
    "allowed_verbs",
    "build_parser",
    "build_request",
    "dispatch",
    "main",
    "parse_payload",
    "resolve_endpoint",
]
