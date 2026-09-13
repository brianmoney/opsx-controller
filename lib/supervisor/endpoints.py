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

import socket
import struct
from dataclasses import dataclass, field
from types import MappingProxyType
from typing import Any, Callable, Iterable, Mapping

ENDPOINT_OPERATOR = "operator"
ENDPOINT_WORKER = "worker-actions"

# This surface never carries operator credential material. Kept as an explicit
# constant so tests can assert the property rather than infer it from silence.
USES_TOKEN_MATERIAL = False


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


def _operator_approve(request: Mapping[str, Any], credentials: PeerCredentials) -> dict[str, Any]:
    return {
        "verb": "approve",
        "operator_uid": credentials.uid,
        "approved": request.get("action_id"),
    }


def _operator_revise_policy(request: Mapping[str, Any], credentials: PeerCredentials) -> dict[str, Any]:
    return {
        "verb": "revise_policy",
        "operator_uid": credentials.uid,
        "revision": request.get("revision"),
    }


def _operator_enable(request: Mapping[str, Any], credentials: PeerCredentials) -> dict[str, Any]:
    return {"verb": "enable", "operator_uid": credentials.uid}


def _operator_cancel(request: Mapping[str, Any], credentials: PeerCredentials) -> dict[str, Any]:
    return {"verb": "cancel", "operator_uid": credentials.uid}


def _worker_report_status(
    request: Mapping[str, Any], credentials: PeerCredentials
) -> dict[str, Any]:
    return {"verb": "report_status", "worker_uid": credentials.uid, "status": request.get("status")}


def _worker_request_action(
    request: Mapping[str, Any], credentials: PeerCredentials
) -> dict[str, Any]:
    return {
        "verb": "request_action",
        "worker_uid": credentials.uid,
        "action": request.get("action"),
    }


def _worker_record_evidence(
    request: Mapping[str, Any], credentials: PeerCredentials
) -> dict[str, Any]:
    return {
        "verb": "record_evidence",
        "worker_uid": credentials.uid,
        "evidence": request.get("evidence"),
    }


def _worker_heartbeat(
    request: Mapping[str, Any], credentials: PeerCredentials
) -> dict[str, Any]:
    return {"verb": "heartbeat", "worker_uid": credentials.uid}


OPERATOR_HANDLERS: Mapping[str, Callable[..., Any]] = MappingProxyType(
    {
        "approve": _operator_approve,
        "revise_policy": _operator_revise_policy,
        "enable": _operator_enable,
        "cancel": _operator_cancel,
    }
)

WORKER_HANDLERS: Mapping[str, Callable[..., Any]] = MappingProxyType(
    {
        "report_status": _worker_report_status,
        "request_action": _worker_request_action,
        "record_evidence": _worker_record_evidence,
        "heartbeat": _worker_heartbeat,
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
