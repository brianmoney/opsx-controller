"""Client transport for the supervision broker over the endpoint split.

The broker lives in the trusted authority domain; the CLI and the supervised
service reach it over one of the two socket surfaces:

- the **operator endpoint**, for OS-authenticated operator mutations, and
- the **worker-actions endpoint**, for the scoped delegated-gate action.

Authentication is the kernel-reported peer uid on the *server* side
(``SO_PEERCRED``). This module only supplies the client half and the
newline-delimited JSON framing; it carries no credential material, never
fabricates a principal, and fails closed with :class:`BrokerUnavailableError`
when the broker path cannot be reached.
"""

from __future__ import annotations

import json
import os
import socket
from pathlib import Path
from typing import Any, Mapping

from lib.supervisor import broker as broker_module
from lib.supervisor import endpoints as endpoints_module

OPERATOR_SOCKET_ENV = "OPSX_SUPERVISOR_OPERATOR_SOCKET"
WORKER_SOCKET_ENV = "OPSX_SUPERVISOR_WORKER_SOCKET"

DEFAULT_TIMEOUT_SECONDS = 30.0

# Named broker errors that may travel back over the wire.
_ERROR_TYPES: dict[str, type[BaseException]] = {
    "BrokerError": broker_module.BrokerError,
    "BrokerMediationError": broker_module.BrokerMediationError,
    "BrokerUnavailableError": broker_module.BrokerUnavailableError,
    "StaleMaterialError": broker_module.StaleMaterialError,
}


def endpoint_socket_path(
    kind: str, *, env: Mapping[str, str] | None = None
) -> Path | None:
    """Return the configured socket path for *kind*, or ``None`` when unset."""
    environment = os.environ if env is None else env
    if kind == endpoints_module.ENDPOINT_OPERATOR:
        configured = environment.get(OPERATOR_SOCKET_ENV, "")
    elif kind == endpoints_module.ENDPOINT_WORKER:
        configured = environment.get(WORKER_SOCKET_ENV, "")
    else:
        raise broker_module.BrokerError(f"unknown endpoint kind: {kind}")
    configured = configured.strip()
    return Path(configured) if configured else None


# ---------------------------------------------------------------------------
# Framing
# ---------------------------------------------------------------------------


def encode_request(request: Mapping[str, Any]) -> bytes:
    return (json.dumps(request, sort_keys=True, default=str) + "\n").encode("utf-8")


def read_json_request(conn: Any) -> Mapping[str, Any]:
    """Read exactly one newline-delimited JSON request from *conn*."""
    raw = _read_line(conn)
    try:
        payload = json.loads(raw)
    except (ValueError, TypeError) as exc:
        raise endpoints_module.EndpointError(
            f"request was not valid JSON: {exc}"
        ) from exc
    if not isinstance(payload, Mapping):
        raise endpoints_module.EndpointError("request must be a JSON object")
    return payload


def write_json_response(conn: Any, response: Mapping[str, Any]) -> None:
    conn.sendall((json.dumps(response, sort_keys=True, default=str) + "\n").encode("utf-8"))


def _read_line(conn: Any) -> str:
    chunks: list[bytes] = []
    while True:
        chunk = conn.recv(4096)
        if not chunk:
            break
        chunks.append(chunk)
        if b"\n" in chunk:
            break
    raw = b"".join(chunks)
    if b"\n" not in raw:
        raise broker_module.BrokerMediationError(
            "the endpoint closed the connection before replying; the peer was "
            "not accepted"
        )
    line, _, _ = raw.partition(b"\n")
    return line.decode("utf-8")


# ---------------------------------------------------------------------------
# Server half (used by the supervised service and by tests)
# ---------------------------------------------------------------------------


def serve_one(
    endpoint: endpoints_module.Endpoint,
    conn: Any,
    *,
    ledger_resolver: Any = None,
) -> dict[str, Any]:
    """Authenticate *conn*, dispatch one request, and write one response.

    *ledger_resolver*, when supplied, maps the raw request to a
    ``(ledger, job_id)`` pair the broker handlers operate on (the
    service-owned ledger is never sent over the wire). Handler exceptions are
    serialized with their type name so the client can raise the matching named
    error; a rejected peer is closed with no response.
    """
    try:
        if ledger_resolver is not None:
            def read_request(connection: Any) -> Mapping[str, Any]:
                request = read_json_request(connection)
                ledger, job_id = ledger_resolver(request)
                enriched = dict(request)
                enriched["ledger"] = ledger
                enriched["job_id"] = job_id
                return enriched
        else:
            read_request = read_json_request
        try:
            result = endpoint.handle(conn, read_request=read_request)
        except endpoints_module.PeerCredentialError:
            # A rejected peer is closed before any request is read; there is
            # no authenticated response to send.
            raise
        except broker_module.BrokerError as exc:
            write_json_response(
                conn,
                {"ok": False, "error": type(exc).__name__, "message": str(exc)},
            )
            return {"ok": False, "error": type(exc).__name__, "message": str(exc)}
        except Exception as exc:  # noqa: BLE001 - surfaced to the client as a named error
            write_json_response(
                conn,
                {"ok": False, "error": "LedgerError", "message": str(exc)},
            )
            raise
        write_json_response(conn, {"ok": True, "result": result})
        return {"ok": True, "result": result}
    finally:
        try:
            conn.close()
        except OSError:  # pragma: no cover - best-effort close
            pass


# ---------------------------------------------------------------------------
# Client half
# ---------------------------------------------------------------------------


def call(
    request: Mapping[str, Any],
    *,
    kind: str = endpoints_module.ENDPOINT_OPERATOR,
    socket_path: os.PathLike[str] | str | None = None,
    env: Mapping[str, str] | None = None,
    timeout: float = DEFAULT_TIMEOUT_SECONDS,
    connector: Any = None,
) -> dict[str, Any]:
    """Send one broker *request* and return the handler result.

    Fails closed: an unset socket path, a missing socket, or an inability to
    connect raises :class:`BrokerUnavailableError`. A connection closed before
    a reply (the peer was not accepted) raises
    :class:`BrokerMediationError`. A named handler error travels back as the
    matching exception type.

    *connector*, when supplied, is a zero-argument callable returning an
    already-connected socket; it is the in-process/socketpair transport seam.
    Otherwise the client connects to the configured Unix socket path.
    """
    conn: Any = None
    if connector is not None:
        try:
            conn = connector()
        except broker_module.BrokerError:
            raise
        except Exception as exc:  # noqa: BLE001 - fail closed
            raise broker_module.BrokerUnavailableError(
                f"could not reach the {kind} broker: {exc}"
            ) from exc
    else:
        path = Path(socket_path) if socket_path is not None else endpoint_socket_path(kind, env=env)
        if path is None:
            raise broker_module.BrokerUnavailableError(
                f"no {kind} broker socket is configured "
                f"({OPERATOR_SOCKET_ENV if kind == endpoints_module.ENDPOINT_OPERATOR else WORKER_SOCKET_ENV}); "
                "a registered supervised job requires the broker to be reachable"
            )
        if not path.exists():
            raise broker_module.BrokerUnavailableError(
                f"the {kind} broker socket {path} does not exist; start the "
                "supervised service before mutating a registered job"
            )
        conn = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        conn.settimeout(timeout)
        try:
            conn.connect(str(path))
        except OSError as exc:
            try:
                conn.close()
            except OSError:  # pragma: no cover - best-effort close
                pass
            raise broker_module.BrokerUnavailableError(
                f"could not reach the {kind} broker at {path}: {exc}"
            ) from exc
    try:
        try:
            conn.sendall(encode_request(request))
            line = _read_line(conn)
        except broker_module.BrokerMediationError:
            raise
        except OSError as exc:
            raise broker_module.BrokerMediationError(
                f"the {kind} broker closed the connection before replying; the "
                f"requesting principal is not accepted: {exc}"
            ) from exc
    finally:
        try:
            conn.close()
        except OSError:  # pragma: no cover - best-effort close
            pass

    try:
        response = json.loads(line)
    except (ValueError, TypeError) as exc:
        raise broker_module.BrokerError(
            f"broker replied with an unparseable response: {exc}"
        ) from exc
    if not isinstance(response, Mapping):
        raise broker_module.BrokerError("broker response must be a JSON object")
    if response.get("ok"):
        result = response.get("result")
        return dict(result) if isinstance(result, Mapping) else {"result": result}
    error_name = str(response.get("error", "BrokerError"))
    message = str(response.get("message", "broker refused the request"))
    error_type = _ERROR_TYPES.get(error_name, broker_module.BrokerError)
    raise error_type(message)


__all__ = [
    "DEFAULT_TIMEOUT_SECONDS",
    "OPERATOR_SOCKET_ENV",
    "WORKER_SOCKET_ENV",
    "call",
    "encode_request",
    "endpoint_socket_path",
    "read_json_request",
    "serve_one",
    "write_json_response",
]
