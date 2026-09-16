"""OpenCode session bridge over the supervised action journal.

The bridge drives a supervised job's primary session against a headless
``opencode serve`` instance over loopback HTTP. It is part of the stdlib-only
``lib/supervisor`` package, so it never imports another runtime package and it
is importable without side effects (no argument parsing, no process spawning,
no ``.opsx-plan/`` access). Cross-module references go through the module
object, matching the package's import discipline.

Contract (documented in ``core/plan-supervision.md``):

- **Documented, versioned API subset.** Five operations — create, prompt,
  result-schema, lookup, and abort — over the documented server surface only
  (:data:`DOCUMENTED_API_SUBSET`). Before any session operation the bridge
  runs a version capability check against ``GET /global/health`` and fails
  closed with :class:`UnsupportedVersionError` for a server outside the pinned
  supported range. An operation requested before a successful check raises
  :class:`CapabilityNotCheckedError`.
- **Identity before the prompt.** :class:`JournaledSessionBridge` commits the
  action intent (carrying the generated request identity) before issuing the
  prompt, embeds the same identity as a machine-readable marker in the prompt
  payload, and enforces at most one in-flight prompt per session. A lost
  acknowledgement is resolved by lookup against the recorded marker, never by
  re-prompting.
- **Events as hints only.** :func:`parse_event_stream` yields typed
  :class:`SessionEventHint` values and tolerates malformed or partial frames.
  The hint consumer never records an outcome; it only schedules or accelerates
  the authoritative poll loop (:func:`poll_until_terminal`), which is the sole
  path to terminal results, usage figures, and the typed result-schema.
- **Bounded briefings.** :func:`compose_briefing` builds reconnect and
  replacement-session context from the ledger, the authority store, budget and
  reservation state, and incident history — never from a transcript replay —
  and bounds it by :func:`bound_sections`, which never sheds blocking items.

The typed result returned for a prompt follows :data:`RESULT_SCHEMA_VERSION`;
its ``usage``/``cost`` shape matches the shape the orchestrator's dispatch
boundary reconciles, so supervisor-primary usage crosses the same budget
boundary as every other supervised model call.
"""

from __future__ import annotations

import http.client
import json
import os
import re
import secrets
import socket
import time
from dataclasses import dataclass, field
from typing import Any, Callable, Iterable, Iterator, Mapping, Sequence

from lib.supervisor import agent_contracts as agent_contracts_mod
from lib.supervisor import broker as broker_mod
from lib.supervisor import budgets as budget_mod
from lib.supervisor import clock as clock_mod
from lib.supervisor import lock as lock_mod
from lib.supervisor import model_policy as model_policy_mod

# ---------------------------------------------------------------------------
# Documented API subset and version capability check
# ---------------------------------------------------------------------------

# The supported ``opencode serve`` range, pinned at implementation time against
# the operator's installed server (1.18.31) and recorded in
# ``core/plan-supervision.md``. A mismatch fails closed naming the range; the
# remedy is to verify against the new server and extend the pin explicitly.
SUPPORTED_SERVER_VERSION_MIN = (1, 18, 0)
SUPPORTED_SERVER_VERSION_MAX = (1, 19, 0)  # exclusive
SUPPORTED_SERVER_VERSION_RANGE = ">=1.18.0 <1.19.0"

HEALTH_PATH = "/global/health"
EVENT_STREAM_PATH = "/event"
EVENT_STREAM_MEDIA_TYPE = "text/event-stream"

# The documented five-operation subset: create, prompt, result-schema, lookup,
# abort. The bridge depends on no server surface outside this table.
DOCUMENTED_API_SUBSET: dict[str, tuple[str, str]] = {
    "health": ("GET", HEALTH_PATH),
    "create": ("POST", "/session"),
    "lookup_session": ("GET", "/session/{session_id}"),
    "lookup_messages": ("GET", "/session/{session_id}/message"),
    "prompt": ("POST", "/session/{session_id}/prompt_async"),
    "abort": ("POST", "/session/{session_id}/abort"),
    "events": ("GET", EVENT_STREAM_PATH),
}

# The prompt request marker: one machine-readable line appended to the prompt
# payload so the request identity stays discoverable through lookup after a
# lost launch acknowledgement. It is deliberately single-line and prefixed.
REQUEST_MARKER_PREFIX = "OPSX-REQUEST-MARKER:"
_REQUEST_MARKER_RE = re.compile(
    re.escape(REQUEST_MARKER_PREFIX) + r"\s*([A-Za-z0-9][A-Za-z0-9._:-]*)"
)

# The typed result-schema returned for a prompt, derived from polled
# authoritative session state (never from the event channel).
RESULT_SCHEMA_VERSION = 1
RESULT_SCHEMA_NAME = "opencode-session-result"

# Journal vocabulary mirrored from the orchestrator dispatch boundary. The
# bridge is stdlib-only and cannot import ``lib.orchestrator``, so the string
# values are pinned here verbatim; the evidence-kind vocabulary is fixed by the
# supervision contract (``stage_result``, ``usage``, ``spawn_loss``,
# ``session_binding``), and an unknown kind is stored but never decisive.
PROMPT_ACTION_KIND = "supervisor_prompt"
SESSION_LINKAGE_ACTION_KIND = "session_linkage"
EVIDENCE_STAGE_RESULT = "stage_result"
EVIDENCE_USAGE = "usage"
EVIDENCE_SPAWN_LOSS = "spawn_loss"
EVIDENCE_SESSION_BINDING = "session_binding"
EVIDENCE_HINT = "session_hint"

# Prompt journal states that count as in flight for one session. An
# unreconciled ``uncertain`` action is blocking state, so it also blocks a new
# prompt rather than being dispatched past. A ``reconciled`` action is still
# guarded: reconciliation resolves the launch uncertainty recorded by the
# evidence, not the prompt's outcome, so a reconciled-but-not-yet-terminal
# prompt keeps the session's one-in-flight slot until a terminal observation
# resolves it.
IN_FLIGHT_ACTION_STATES = ("intent", "dispatched", "uncertain", "reconciled")

# Briefing bounds. Blocking items are never shed; only the oldest non-blocking
# detail is dropped, and an adopted session receives the smaller re-brief.
BRIEFING_BOUND_CHARS = 6000
REBRIEF_BOUND_CHARS = 2000
BRIEFING_SCHEMA_VERSION = 1


class SessionBridgeError(Exception):
    """Base class for session-bridge failures."""


class UnsupportedVersionError(SessionBridgeError):
    """The server's reported version is outside the documented supported range."""


class UnreachableServerError(SessionBridgeError):
    """The health endpoint could not be reached or did not report healthy."""


class TransportError(SessionBridgeError):
    """A bridge request failed at the transport layer."""


class CapabilityNotCheckedError(SessionBridgeError):
    """A session operation was requested before a successful version check."""


class SessionNotFoundError(SessionBridgeError):
    """The server has no such session."""


class PromptInFlightError(SessionBridgeError):
    """A second prompt was requested while one is still in flight."""


class BridgeJournalError(SessionBridgeError):
    """A journaled bridge lifecycle step failed."""


# ---------------------------------------------------------------------------
# Version parsing and the capability check
# ---------------------------------------------------------------------------


def parse_server_version(text: Any) -> tuple[int, int, int] | None:
    """Parse a server version string into a comparable 3-tuple, or ``None``.

    A malformed or empty version is ``None`` — never treated as matching.
    """
    if not isinstance(text, str):
        return None
    match = re.search(r"(\d+)\.(\d+)(?:\.(\d+))?", text.strip())
    if match is None:
        return None
    major, minor, patch = match.group(1), match.group(2), match.group(3) or "0"
    try:
        return (int(major), int(minor), int(patch))
    except (TypeError, ValueError):  # pragma: no cover - regex guarantees digits
        return None


def version_in_supported_range(
    version: tuple[int, int, int],
    *,
    minimum: tuple[int, int, int] = SUPPORTED_SERVER_VERSION_MIN,
    maximum: tuple[int, int, int] = SUPPORTED_SERVER_VERSION_MAX,
) -> bool:
    """True when *version* is inside ``[minimum, maximum)``."""
    return minimum <= version < maximum


@dataclass(frozen=True)
class SessionCapability:
    """The recorded result of a successful version capability check."""

    version: str
    version_tuple: tuple[int, int, int]
    supported_range: str
    checked_at: str

    def as_dict(self) -> dict[str, Any]:
        return {
            "version": self.version,
            "version_tuple": list(self.version_tuple),
            "supported_range": self.supported_range,
            "checked_at": self.checked_at,
        }


# ---------------------------------------------------------------------------
# Loopback HTTP transport
# ---------------------------------------------------------------------------

_LOOPBACK_HOSTS = frozenset({"127.0.0.1", "::1", "localhost"})

# Only idempotent methods may be retried after a stale keep-alive connection.
# A prompt is a side effect: retrying it could duplicate a prompt the server
# already accepted, which is exactly the failure the request identity exists to
# prevent. The bridge therefore never blind-retries a mutating request.
_IDEMPOTENT_METHODS = frozenset({"GET", "HEAD"})


def is_loopback_host(host: Any) -> bool:
    """True when *host* is a loopback address this bridge will talk to."""
    if not isinstance(host, str) or not host.strip():
        return False
    candidate = host.strip().strip("[]")
    if candidate in _LOOPBACK_HOSTS:
        return True
    if candidate.startswith("127."):
        parts = candidate.split(".")
        return len(parts) == 4 and all(part.isdigit() for part in parts)
    return False


def parse_loopback_address(address: Any) -> tuple[str, int]:
    """Split a ``host:port`` loopback address, refusing a non-loopback host."""
    if not isinstance(address, str) or not address.strip():
        raise TransportError(f"invalid session server address: {address!r}")
    candidate = address.strip()
    if candidate.startswith("["):  # bracketed IPv6, e.g. [::1]:4096
        host, _, port = candidate[1:].partition("]:")
    else:
        host, _, port = candidate.rpartition(":")
    if not host or not port.isdigit():
        raise TransportError(f"invalid session server address: {address!r}")
    if not is_loopback_host(host):
        raise TransportError(
            f"refusing to talk to non-loopback session server {address!r}; the "
            "session bridge is loopback-only"
        )
    return host, int(port)


@dataclass(frozen=True)
class HTTPResponse:
    """One transport response: status, headers, and raw body."""

    status: int
    headers: Mapping[str, str] = field(default_factory=dict)
    body: bytes = b""

    @property
    def text(self) -> str:
        return self.body.decode("utf-8", errors="replace")

    @property
    def ok(self) -> bool:
        return 200 <= self.status < 300

    def json(self) -> Any:
        if not self.body:
            return None
        try:
            return json.loads(self.text)
        except (TypeError, ValueError) as exc:
            raise TransportError(f"response body was not JSON: {exc}") from exc


class LoopbackTransport:
    """Minimal stdlib ``http.client`` transport pinned to loopback.

    The transport is the bridge's only I/O surface. It refuses a non-loopback
    address at construction and translates every socket/protocol failure into
    :class:`TransportError`/``UnreachableServerError`` so callers never see a
    raw ``OSError``. ``connection_factory`` is an injection seam for tests.
    """

    def __init__(
        self,
        host: str,
        port: int,
        *,
        timeout: float = 5.0,
        connection_factory: Callable[..., Any] | None = None,
    ) -> None:
        if not is_loopback_host(host):
            raise TransportError(
                f"refusing to talk to non-loopback session server {host!r}; the "
                "session bridge is loopback-only"
            )
        self.host = host
        self.port = int(port)
        self.timeout = float(timeout)
        self._factory = connection_factory or http.client.HTTPConnection
        self._connection: Any = None

    @classmethod
    def from_address(cls, address: str, **kwargs: Any) -> "LoopbackTransport":
        host, port = parse_loopback_address(address)
        return cls(host, port, **kwargs)

    @property
    def address(self) -> str:
        return f"{self.host}:{self.port}"

    def _connect(self, *, fresh: bool = False) -> Any:
        if self._connection is None or fresh:
            self._close_quietly()
            try:
                self._connection = self._factory(
                    self.host, self.port, timeout=self.timeout
                )
            except Exception as exc:  # noqa: BLE001 - every connect failure is named
                raise UnreachableServerError(
                    f"the session server at {self.address} is unreachable: {exc}"
                ) from exc
        return self._connection

    def request(
        self,
        method: str,
        path: str,
        *,
        body: Any = None,
        headers: Mapping[str, str] | None = None,
    ) -> HTTPResponse:
        """Issue one request; retry once on a stale keep-alive connection.

        Only idempotent requests are retried. A mutating request (the prompt)
        is issued at most once: a transport failure raises
        :class:`TransportError` so the caller resolves it through lookup
        against the durable request identity instead of issuing a second
        prompt.
        """
        payload: bytes | None = None
        request_headers = dict(headers or {})
        if body is not None:
            payload = json.dumps(body).encode("utf-8")
            request_headers.setdefault("content-type", "application/json")
        request_headers.setdefault("accept", "application/json")
        retryable = method.upper() in _IDEMPOTENT_METHODS
        last_error: BaseException | None = None
        attempts = (1, 2) if retryable else (1,)
        for attempt in attempts:
            connection = self._connect(fresh=attempt == 2)
            try:
                connection.request(method, path, body=payload, headers=request_headers)
                response = connection.getresponse()
                raw = response.read()
                return HTTPResponse(
                    status=int(response.status),
                    headers={k.lower(): v for k, v in response.getheaders()},
                    body=raw or b"",
                )
            except (http.client.HTTPException, OSError) as exc:
                last_error = exc
                self._close_quietly()
                continue
        if isinstance(last_error, (ConnectionRefusedError, socket.gaierror)):
            raise UnreachableServerError(
                f"the session server at {self.address} is unreachable: {last_error}"
            ) from last_error
        raise TransportError(
            f"{method} {path} against {self.address} failed at the transport "
            f"layer: {last_error}"
        ) from last_error

    def open_stream(
        self, path: str, *, headers: Mapping[str, str] | None = None
    ) -> Iterator[bytes]:
        """Yield raw chunks from a streaming GET (the SSE event channel).

        The generator owns a dedicated connection: SSE is a long-lived
        response, so it never shares the request connection pool.
        """
        connection: Any = None
        try:
            connection = self._factory(self.host, self.port, timeout=self.timeout)
            request_headers = dict(headers or {})
            request_headers.setdefault("accept", EVENT_STREAM_MEDIA_TYPE)
            connection.request("GET", path, headers=request_headers)
            response = connection.getresponse()
            while True:
                # An SSE frame is line-oriented and may arrive in arbitrary
                # transport chunks; reading one line at a time keeps the
                # parser's blank-line framing exact without waiting for a
                # fixed-size read to fill.
                line = response.readline()
                if not line:
                    break
                yield line
        except (http.client.HTTPException, OSError) as exc:
            raise TransportError(
                f"the event stream at {self.address} was lost: {exc}"
            ) from exc
        finally:
            try:
                if connection is not None:
                    connection.close()
            except Exception:  # pragma: no cover - best-effort close
                pass

    def _close_quietly(self) -> None:
        connection, self._connection = self._connection, None
        if connection is None:
            return
        try:
            connection.close()
        except Exception:  # pragma: no cover - best-effort close
            pass

    def close(self) -> None:
        self._close_quietly()


# ---------------------------------------------------------------------------
# Session API subset and typed result-schema
# ---------------------------------------------------------------------------


def new_request_id() -> str:
    """Return a fresh, discoverable request identity."""
    return "req_" + secrets.token_hex(16)


def request_marker(request_id: str) -> str:
    """Render the machine-readable marker line for *request_id*."""
    return f"{REQUEST_MARKER_PREFIX}{request_id}"


def extract_marker(text: Any) -> str | None:
    """Return the request identity carried by *text*, or ``None``."""
    if not isinstance(text, str):
        return None
    match = _REQUEST_MARKER_RE.search(text)
    return match.group(1) if match is not None else None


def parse_model_identity(model: Any) -> dict[str, str] | None:
    """Split a pinned ``provider/model`` identity into the server's shape."""
    if not isinstance(model, str) or "/" not in model:
        return None
    provider, model_id = model.split("/", 1)
    provider, model_id = provider.strip(), model_id.strip()
    if not provider or not model_id:
        return None
    return {"providerID": provider, "modelID": model_id}


def _text_parts(messages: Sequence[Any]) -> list[str]:
    texts: list[str] = []
    for entry in messages:
        if not isinstance(entry, Mapping):
            continue
        for part in entry.get("parts") or ():
            if isinstance(part, Mapping) and isinstance(part.get("text"), str):
                texts.append(part["text"])
    return texts


def find_marker(messages: Sequence[Any]) -> str | None:
    """Return the first request marker found in *messages*, or ``None``."""
    for text in _text_parts(messages):
        marker = extract_marker(text)
        if marker is not None:
            return marker
    return None


def _message_info(entry: Any) -> Mapping[str, Any]:
    if not isinstance(entry, Mapping):
        return {}
    info = entry.get("info")
    return info if isinstance(info, Mapping) else {}


def _message_order(entry: Any) -> tuple[int, str]:
    info = _message_info(entry)
    created = (info.get("time") or {}).get("created") if isinstance(info.get("time"), Mapping) else None
    try:
        return (int(created), str(info.get("id") or ""))
    except (TypeError, ValueError):
        return (0, str(info.get("id") or ""))


def result_usage(assistant: Mapping[str, Any]) -> dict[str, Any]:
    """Build the usage payload from an assistant message's reported figures."""
    tokens = assistant.get("tokens") if isinstance(assistant.get("tokens"), Mapping) else {}
    cache = tokens.get("cache") if isinstance(tokens.get("cache"), Mapping) else {}
    usage_available = any(
        key in tokens for key in ("input", "output", "reasoning", "total")
    )
    return {
        "usage_available": bool(usage_available),
        "input_tokens": int(tokens.get("input") or 0) if usage_available else None,
        "output_tokens": int(tokens.get("output") or 0) if usage_available else None,
        "cached_input_tokens": int(cache.get("read") or 0) if usage_available else None,
        "reasoning_tokens": int(tokens.get("reasoning") or 0) if usage_available else None,
    }


def result_cost(assistant: Mapping[str, Any]) -> dict[str, Any]:
    """Build the cost payload from an assistant message, or mark it unavailable."""
    reported = assistant.get("cost")
    if isinstance(reported, (int, float)) and not isinstance(reported, bool):
        return {"status": "estimated", "estimated_cost": float(reported)}
    return {"status": "unavailable", "estimated_cost": None}


def _result_status(
    assistant: Mapping[str, Any], *, assistant_present: bool
) -> str:
    if not assistant_present:
        return "pending"
    if assistant.get("error"):
        error = assistant.get("error")
        if isinstance(error, Mapping) and "aborted" in str(error.get("name") or "").lower():
            return "aborted"
        return "error"
    times = assistant.get("time") if isinstance(assistant.get("time"), Mapping) else {}
    if times.get("completed") is not None:
        return "completed"
    return "pending"


def parse_result_schema(
    messages: Sequence[Any],
    *,
    session_id: str | None = None,
    marker: str | None = None,
) -> dict[str, Any]:
    """Derive the typed prompt result from polled authoritative message state.

    The result is computed only from the messages the server returns: the
    marker's user message identifies the prompt (when *marker* is given) and
    the newest assistant reply to it supplies the terminal state, usage
    figures, and error classification. Nothing here consults the event
    channel, so a lost, duplicate, or reordered event cannot fabricate an
    outcome.

    A requested marker that is not present in the returned messages leaves the
    result ``pending`` (``marker_found`` false, ``terminal`` false): an older,
    unrelated completed turn belongs to some other request and must never be
    reported as this marker's terminal outcome.
    """
    ordered = sorted((e for e in messages if isinstance(e, Mapping)), key=_message_order)
    requested_marker = marker
    user_id: str | None = None
    marker_found = False
    for entry in ordered:
        info = _message_info(entry)
        if info.get("role") != "user":
            continue
        entry_marker = find_marker([entry])
        if requested_marker is None:
            # No marker requested: the newest user turn is the subject, and the
            # discovered marker (if any) is reported as-is.
            user_id = str(info.get("id") or "") or None
            marker = entry_marker
            marker_found = entry_marker is not None
            break
        if entry_marker != requested_marker:
            continue
        # Scope the lookup to the matched marker's user message so no other
        # turn's assistant reply can be selected.
        user_id = str(info.get("id") or "") or None
        marker_found = True
        break
    if requested_marker is not None and not marker_found:
        # The requested marker is absent from authoritative state. Whatever
        # turns the session does contain belong to earlier requests, so the
        # requested prompt has no observable outcome yet: report pending and
        # select no assistant reply rather than borrowing another turn's.
        return {
            "version": RESULT_SCHEMA_VERSION,
            "schema": RESULT_SCHEMA_NAME,
            "session_id": session_id,
            "marker": marker,
            "marker_found": False,
            "status": "pending",
            "terminal": False,
            "message_id": None,
            "provider_id": None,
            "model_id": None,
            "text": "",
            "usage": result_usage({}),
            "cost": result_cost({}),
            "duration_ms": None,
            "source": "poll",
        }
    assistants: list[Mapping[str, Any]] = []
    for entry in ordered:
        info = _message_info(entry)
        if info.get("role") != "assistant":
            continue
        if user_id is not None and str(info.get("parentID") or "") != user_id:
            continue
        assistants.append(info)
    assistant = assistants[-1] if assistants else {}
    status = _result_status(assistant, assistant_present=bool(assistants))
    text_parts: list[str] = []
    if assistants:
        latest_id = assistant.get("id")
        for entry in ordered:
            if _message_info(entry).get("id") != latest_id:
                continue
            for part in entry.get("parts") or ():
                if isinstance(part, Mapping) and part.get("type") == "text":
                    if isinstance(part.get("text"), str):
                        text_parts.append(part["text"])
    times = assistant.get("time") if isinstance(assistant.get("time"), Mapping) else {}
    duration_ms: int | None = None
    if times.get("created") is not None and times.get("completed") is not None:
        try:
            duration_ms = int(times["completed"]) - int(times["created"])
        except (TypeError, ValueError):  # pragma: no cover - defensive
            duration_ms = None
    return {
        "version": RESULT_SCHEMA_VERSION,
        "schema": RESULT_SCHEMA_NAME,
        "session_id": session_id,
        "marker": marker,
        "marker_found": marker_found,
        "status": status,
        "terminal": status != "pending",
        "message_id": str(assistant.get("id") or "") or None,
        "provider_id": assistant.get("providerID"),
        "model_id": assistant.get("modelID"),
        "text": "\n".join(text_parts).strip(),
        "usage": result_usage(assistant),
        "cost": result_cost(assistant),
        "duration_ms": duration_ms,
        "source": "poll",
    }


class SessionBridge:
    """The documented session API subset behind one version capability check.

    Every operation refuses when the check has not succeeded
    (:class:`CapabilityNotCheckedError`), so no session request can be issued
    against an unchecked or unsupported server.
    """

    def __init__(
        self,
        transport: LoopbackTransport,
        *,
        supported_min: tuple[int, int, int] = SUPPORTED_SERVER_VERSION_MIN,
        supported_max: tuple[int, int, int] = SUPPORTED_SERVER_VERSION_MAX,
        supported_range: str = SUPPORTED_SERVER_VERSION_RANGE,
        now: Callable[[], str] | None = None,
    ) -> None:
        self.transport = transport
        self.supported_min = supported_min
        self.supported_max = supported_max
        self.supported_range = supported_range
        self._now = now  # resolved through the clock module at call time
        self._capability: SessionCapability | None = None

    # -- capability check ------------------------------------------------

    @property
    def capability(self) -> SessionCapability | None:
        return self._capability

    @property
    def is_capable(self) -> bool:
        return self._capability is not None

    def check_capability(self) -> SessionCapability:
        """Query ``GET /global/health`` and pin the server version.

        Fails closed with :class:`UnreachableServerError` when the health
        endpoint cannot be reached or does not report healthy, and with
        :class:`UnsupportedVersionError` when the reported version is missing,
        unparseable, or outside the documented supported range.
        """
        response = self.transport.request(*DOCUMENTED_API_SUBSET["health"])
        if response.status != 200:
            raise UnreachableServerError(
                f"the session server health check returned HTTP {response.status}; "
                "refusing to use the bridge"
            )
        try:
            payload = response.json()
        except TransportError as exc:
            raise UnreachableServerError(
                f"the session server health check was unreadable: {exc}"
            ) from exc
        if not isinstance(payload, Mapping):
            raise UnreachableServerError(
                "the session server health check did not report a JSON object"
            )
        if payload.get("healthy") is not True:
            raise UnreachableServerError(
                f"the session server reported unhealthy: {payload!r}"
            )
        reported = payload.get("version")
        parsed = parse_server_version(reported)
        if parsed is None or not version_in_supported_range(
            parsed, minimum=self.supported_min, maximum=self.supported_max
        ):
            raise UnsupportedVersionError(
                f"the session server version {reported!r} is outside the "
                f"documented supported range {self.supported_range}; refusing all "
                "session operations until the bridge is verified against the "
                "server and the range is extended explicitly"
            )
        self._capability = SessionCapability(
            version=str(reported),
            version_tuple=parsed,
            supported_range=self.supported_range,
            checked_at=(self._now or clock_mod.utcnow)(),
        )
        return self._capability

    def require_capability(self) -> SessionCapability:
        if self._capability is None:
            raise CapabilityNotCheckedError(
                "the session bridge version capability check has not succeeded; "
                "refusing to issue a session operation against an unchecked server"
            )
        return self._capability

    def _path(self, key: str, session_id: str | None = None) -> str:
        method, template = DOCUMENTED_API_SUBSET[key]
        return template.format(session_id=session_id)

    def _checked_request(
        self, key: str, *, session_id: str | None = None, body: Any = None
    ) -> HTTPResponse:
        self.require_capability()
        method, _template = DOCUMENTED_API_SUBSET[key]
        return self.transport.request(method, self._path(key, session_id), body=body)

    # -- the five operations ---------------------------------------------

    def create_session(
        self,
        *,
        title: str | None = None,
        agent: str | None = None,
        model: Mapping[str, Any] | None = None,
    ) -> dict[str, Any]:
        """Create a session; returns the server's session object."""
        body: dict[str, Any] = {}
        if title:
            body["title"] = title
        if agent:
            body["agent"] = agent
        if model:
            body["model"] = dict(model)
        response = self._checked_request("create", body=body or None)
        if not response.ok:
            raise TransportError(
                f"session create failed with HTTP {response.status}: {response.text[:200]}"
            )
        payload = response.json()
        if not isinstance(payload, Mapping) or not payload.get("id"):
            raise TransportError("session create returned no session identity")
        return dict(payload)

    def prompt_async(
        self,
        session_id: str,
        *,
        text: str,
        marker: str | None = None,
        model: Mapping[str, Any] | None = None,
        agent: str | None = None,
        message_id: str | None = None,
    ) -> str:
        """Issue one async prompt, carrying *marker* in the payload.

        Returns the marker that was embedded. A transport failure raises
        :class:`TransportError`; the caller must resolve the prompt through
        lookup rather than re-prompting (the request identity is durable in the
        journal before this call).
        """
        if not isinstance(text, str) or not text.strip():
            raise BridgeJournalError("a prompt requires non-empty text")
        body: dict[str, Any] = {}
        if model:
            body["model"] = dict(model)
        if agent:
            body["agent"] = agent
        if message_id:
            body["messageID"] = message_id
        prompt_text = text if marker is None else f"{text}\n\n{request_marker(marker)}"
        body["parts"] = [{"type": "text", "text": prompt_text}]
        response = self._checked_request(
            "prompt", session_id=session_id, body=body
        )
        if response.status == 404:
            raise SessionNotFoundError(
                f"the session server has no session {session_id!r}"
            )
        if not response.ok:
            raise TransportError(
                f"prompt against session {session_id!r} failed with HTTP "
                f"{response.status}: {response.text[:200]}"
            )
        return marker or ""

    def lookup_session(self, session_id: str) -> dict[str, Any] | None:
        """Return the session object, or ``None`` when the server has none."""
        response = self._checked_request("lookup_session", session_id=session_id)
        if response.status == 404:
            return None
        if not response.ok:
            raise TransportError(
                f"session {session_id!r} lookup failed with HTTP {response.status}"
            )
        payload = response.json()
        return dict(payload) if isinstance(payload, Mapping) else None

    def lookup_messages(self, session_id: str) -> list[Any]:
        """Return the authoritative message list for *session_id*."""
        response = self._checked_request("lookup_messages", session_id=session_id)
        if response.status == 404:
            raise SessionNotFoundError(
                f"the session server has no session {session_id!r}"
            )
        if not response.ok:
            raise TransportError(
                f"message lookup for session {session_id!r} failed with HTTP "
                f"{response.status}"
            )
        payload = response.json()
        return list(payload) if isinstance(payload, list) else []

    def abort(self, session_id: str) -> bool:
        """Abort the session; returns the server's boolean acknowledgement."""
        response = self._checked_request("abort", session_id=session_id)
        if response.status == 404:
            raise SessionNotFoundError(
                f"the session server has no session {session_id!r}"
            )
        if not response.ok:
            raise TransportError(
                f"abort of session {session_id!r} failed with HTTP {response.status}"
            )
        payload = response.json()
        return bool(payload) if payload is not None else True

    def result(self, session_id: str, *, marker: str | None = None) -> dict[str, Any]:
        """Poll authoritative state and return the typed result-schema.

        Both authoritative surfaces are consulted: the session record and the
        message list. A session the server no longer has is terminal
        (``aborted``) — the run cannot still be in flight — so a vanished
        session converges instead of polling forever.
        """
        session = self.lookup_session(session_id)
        if session is None:
            result = parse_result_schema(
                [], session_id=session_id, marker=marker
            )
            result["status"] = "aborted"
            result["terminal"] = True
            return result
        messages = self.lookup_messages(session_id)
        return parse_result_schema(messages, session_id=session_id, marker=marker)

    def open_event_stream(self) -> Iterator[bytes]:
        """Yield raw chunks from the documented event stream (hints only)."""
        self.require_capability()
        return self.transport.open_stream(self._path("events"))


class _PendingObservation(Exception):
    """Internal: a non-terminal poll observation, retried on the bound schedule."""


def poll_until_terminal(
    bridge: SessionBridge,
    session_id: str,
    *,
    marker: str | None = None,
    sleep: Callable[[float], None] = time.sleep,
    max_attempts: int = budget_mod.BACKOFF_MAX_ATTEMPTS,
    on_poll: Callable[[int, dict[str, Any]], None] | None = None,
) -> dict[str, Any]:
    """Poll the authoritative session API until a terminal result or the bound.

    This is the sole path to terminal results, usage figures, and the typed
    result-schema. The loop is driven by the budget module's bounded-backoff
    helper: a still-pending observation is the retryable condition, so the
    schedule is the shared one (capped exponential delay, bounded attempts).
    Exhausting the bound returns the still-pending observation so the caller
    records uncertainty rather than polling forever.
    """
    if max_attempts < 1:
        raise BridgeJournalError("poll loop requires at least one attempt")
    state: dict[str, Any] = {
        "result": parse_result_schema([], session_id=session_id, marker=marker),
        "attempt": 0,
    }

    def _observed() -> dict[str, Any]:
        state["attempt"] = int(state["attempt"]) + 1
        result = bridge.result(session_id, marker=marker)
        state["result"] = result
        if on_poll is not None:
            on_poll(int(state["attempt"]), result)
        if result["terminal"]:
            return result
        raise _PendingObservation()

    try:
        return budget_mod.run_with_bounded_backoff(
            _observed,
            sleep=sleep,
            should_retry=lambda exc: isinstance(exc, _PendingObservation),
            max_attempts=max_attempts,
        )
    except _PendingObservation:
        return state["result"]


# ---------------------------------------------------------------------------
# Events-as-hints consumer and tolerant text/event-stream parser
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class SessionEventHint:
    """One typed hint parsed from the event stream."""

    event_id: str
    kind: str
    session_id: str | None = None
    message_id: str | None = None
    part_id: str | None = None
    payload: Mapping[str, Any] = field(default_factory=dict)

    @property
    def identity(self) -> tuple[str, ...] | None:
        """A stable identity for duplicate recognition, or ``None``."""
        if self.message_id and self.part_id:
            return ("part", self.message_id, self.part_id)
        if self.message_id:
            return ("message", self.message_id)
        if self.event_id:
            return ("event", self.event_id)
        return None


def parse_event_frame(block: str) -> SessionEventHint | None:
    """Parse one complete SSE frame, or ``None`` when malformed/tolerated.

    A frame with no ``data:`` line, unparseable JSON, or a non-object payload
    yields ``None``: a malformed frame is skipped, never fatal.
    """
    data_lines: list[str] = []
    event_name = ""
    for raw_line in block.split("\n"):
        line = raw_line.rstrip("\r")
        if not line or line.startswith(":"):
            continue
        field_name, _, value = line.partition(":")
        value = value[1:] if value.startswith(" ") else value
        if field_name == "data":
            data_lines.append(value)
        elif field_name == "event":
            event_name = value.strip()
    if not data_lines:
        return None
    try:
        payload = json.loads("\n".join(data_lines))
    except (TypeError, ValueError):
        return None
    if not isinstance(payload, Mapping):
        return None
    properties = payload.get("properties")
    properties = properties if isinstance(properties, Mapping) else {}
    kind = payload.get("type") or event_name or "unknown"
    return SessionEventHint(
        event_id=str(payload.get("id") or ""),
        kind=str(kind),
        session_id=properties.get("sessionID") or payload.get("sessionID"),
        message_id=properties.get("messageID"),
        part_id=properties.get("partID"),
        payload=payload,
    )


def parse_event_stream(chunks: Iterable[bytes | str]) -> Iterator[SessionEventHint]:
    """Yield typed hints from a minimal, tolerant ``text/event-stream`` parser.

    Frames are delimited by a blank line. A partial trailing frame is held
    until its delimiter arrives and is dropped (not raised) at end of stream,
    so a truncated final frame never fails the poll path.
    """
    buffer = ""
    for chunk in chunks:
        text = chunk.decode("utf-8", errors="replace") if isinstance(chunk, bytes) else str(chunk)
        buffer += text.replace("\r\n", "\n").replace("\r", "\n")
        while "\n\n" in buffer:
            block, _, buffer = buffer.partition("\n\n")
            hint = parse_event_frame(block)
            if hint is not None:
                yield hint
    hint = parse_event_frame(buffer)
    if hint is not None:
        yield hint


class HintConsumer:
    """Consumes the event channel as hints only.

    Hints never become outcomes: each accepted hint is deduplicated by
    message/part identity and only schedules or accelerates polling. Stream
    loss converges by polling authoritative state; no replay is requested and
    no missed event's contents are assumed.
    """

    def __init__(self, *, session_id: str | None = None) -> None:
        self.session_id = session_id
        self.hints_seen = 0
        self.duplicates = 0
        self.orphans = 0
        self.stream_lost = False
        self.recorded_outcomes = 0  # invariant: hints never record outcomes
        self._seen: set[tuple[str, ...]] = set()
        self._buffer = ""

    def feed(self, chunk: bytes | str) -> list[SessionEventHint]:
        """Feed one raw stream chunk; return the complete frames it produced.

        A partial trailing frame is retained until its blank-line delimiter
        arrives, so chunk boundaries never split a frame into two events.
        """
        text = (
            chunk.decode("utf-8", errors="replace")
            if isinstance(chunk, bytes)
            else str(chunk)
        )
        self._buffer += text.replace("\r\n", "\n").replace("\r", "\n")
        frames: list[SessionEventHint] = []
        while "\n\n" in self._buffer:
            block, _, self._buffer = self._buffer.partition("\n\n")
            hint = parse_event_frame(block)
            if hint is not None:
                frames.append(hint)
        return frames

    def observe(self, hint: SessionEventHint) -> bool:
        """Accept a hint once; returns ``True`` for a new, relevant hint."""
        if self.session_id is not None and hint.session_id not in (None, self.session_id):
            # A hint about something other than this session is an orphan:
            # counted as an observation, never applied to an action.
            self.orphans += 1
            return False
        self.hints_seen += 1
        identity = hint.identity
        if identity is not None:
            if identity in self._seen:
                self.duplicates += 1
                return False
            self._seen.add(identity)
        return True

    def consume(self, source: Iterable[Any]) -> list[SessionEventHint]:
        """Consume a live hint stream until it ends or fails.

        *source* may already be typed hints or raw ``text/event-stream``
        chunks straight from :meth:`SessionBridge.open_event_stream`; raw
        chunks are parsed with the tolerant parser, which drops malformed or
        partial frames rather than failing. Either way, the end of the stream
        (clean or failed) means the hint channel is no longer delivering, so
        the consumer records ``stream_lost`` and the caller converges on
        authoritative session state by polling instead of waiting for an event
        that will never arrive.
        """
        accepted: list[SessionEventHint] = []
        try:
            for item in source:
                if isinstance(item, SessionEventHint):
                    hints: Iterable[SessionEventHint] = (item,)
                else:
                    hints = self.feed(item)
                for hint in hints:
                    if self.observe(hint):
                        accepted.append(hint)
        except TransportError:
            pass
        finally:
            # A truncated final frame at end of stream is dropped, never
            # raised: the poll path must not fail on a partial frame.
            self.stream_lost = True
        return accepted

    def converge(self, bridge: SessionBridge, session_id: str, *, marker: str | None = None,
                 sleep: Callable[[float], None] = time.sleep,
                 max_attempts: int = budget_mod.BACKOFF_MAX_ATTEMPTS) -> dict[str, Any]:
        """Converge on authoritative state by polling after stream loss.

        Used on disconnect and after a reconnect gap: the bridge polls rather
        than requesting replay or assuming what the missed events contained.
        """
        self.stream_lost = True
        return poll_until_terminal(
            bridge, session_id, marker=marker, sleep=sleep, max_attempts=max_attempts
        )


# ---------------------------------------------------------------------------
# Journal integration: identity before prompt, lost-ack recovery
# ---------------------------------------------------------------------------


def serialize_process_identity(pid: int) -> str:
    """Serialize a process identity exactly as the dispatch boundary does."""
    pid = int(pid)
    process_start = lock_mod.process_start_time(pid)
    boot_id = lock_mod.boot_identity()
    if pid <= 0 or process_start is None or not boot_id:
        raise BridgeJournalError(
            f"process {pid} has no fenceable start/boot identity"
        )
    return json.dumps(
        {"pid": pid, "process_start": process_start, "boot_id": boot_id},
        sort_keys=True,
    )


def parse_process_identity(value: Any) -> dict[str, Any] | None:
    """Decode a serialized process identity, or ``None`` when malformed."""
    decoded: Any = value
    if isinstance(value, str):
        if not value:
            return None
        try:
            decoded = json.loads(value)
        except (TypeError, ValueError):
            return None
    if not isinstance(decoded, Mapping):
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


@dataclass(frozen=True)
class PromptIdentity:
    """The durable identity of one journaled prompt."""

    request_id: str
    action_id: int
    session_id: str
    reservation_id: int | None = None
    acknowledged: bool = True

    @property
    def marker(self) -> str:
        return request_marker(self.request_id)


@dataclass(frozen=True)
class PrimarySessionLinkage:
    """The primary session linkage an interactive chat starts or attaches to."""

    job_id: int
    server_address: str
    session_id: str
    process_id: str | None = None
    recorded_at: str = ""

    def as_dict(self) -> dict[str, Any]:
        return {
            "job_id": self.job_id,
            "server_address": self.server_address,
            "session_id": self.session_id,
            "process_id": self.process_id,
            "recorded_at": self.recorded_at,
        }


class JournaledSessionBridge:
    """Wires every side-effecting bridge operation through the action journal.

    The lifecycle is the journal's, not a parallel one: intent committed in its
    own transaction before any server request, a dispatch record carrying the
    session and server process identity, evidence, and a terminal outcome or an
    explicit uncertain mark with reconciliation.
    """

    def __init__(
        self,
        bridge: SessionBridge,
        ledger: Any,
        *,
        job_id: int,
        run_id: str,
        policy: Mapping[str, Any] | None = None,
        process_id: str | None = None,
        change_id: str | None = None,
        server_identity: str | None = None,
        now: Callable[[], str] = clock_mod.utcnow,
    ) -> None:
        self.bridge = bridge
        self.ledger = ledger
        self.job_id = int(job_id)
        self.run_id = str(run_id)
        self.policy = policy
        self.process_id = process_id
        self.change_id = change_id
        # The launched service-owned session server's fenceable identity. It
        # defaults to the dispatched server process identity; it is what binds
        # the isolated-transport decision to the launched server rather than to
        # a bare loopback hostname.
        self.server_identity = server_identity
        # Immutable service-owned transport authority: the launched server's
        # fenceable identity (captured by the service at launch) plus the
        # address of the transport the service created. Captured once here and
        # never rebuilt from caller input, so no prompt caller can substitute a
        # target, address, or identity.
        self.server_address = getattr(
            getattr(bridge, "transport", None), "address", None
        )
        self.server_binding = agent_contracts_mod.capture_launched_server_binding(
            self.server_address,
            server_identity if server_identity is not None else process_id,
        )
        self._now = now

    # -- journal queries -------------------------------------------------

    def in_flight_prompt(self, session_id: str | None = None) -> Any | None:
        """Return the job's in-flight/unreconciled prompt action, or ``None``."""
        for row in self.ledger.list_actions(self.job_id):
            if row["kind"] != PROMPT_ACTION_KIND:
                continue
            if row["state"] not in IN_FLIGHT_ACTION_STATES:
                continue
            detail = _decode_detail(row["detail"])
            if session_id is not None and detail.get("session_id") not in (None, session_id):
                continue
            return row
        return None

    def action_for_request(self, request_id: str) -> Any | None:
        """Return the prompt action carrying *request_id*, or ``None``."""
        for row in self.ledger.list_actions(self.job_id):
            if row["kind"] != PROMPT_ACTION_KIND:
                continue
            if _decode_detail(row["detail"]).get("request_id") == request_id:
                return row
        return None

    def command_marker_for_session(self, session_id: str) -> str | None:
        row = self.in_flight_prompt(session_id)
        if row is None:
            return None
        return _decode_detail(row["detail"]).get("request_id")

    # -- agent contract enforcement --------------------------------------

    def create_session(
        self,
        *,
        title: str | None = None,
        role: str = model_policy_mod.SUPERVISOR_ROLE,
        model: Mapping[str, Any] | None = None,
        requested_permissions: Sequence[str] | None = None,
    ) -> dict[str, Any]:
        """Create a session bound to *role*'s concrete agent, contract-checked.

        The bridge asserts the agent it binds is the role's registered agent
        before any session request: a session cannot run under a different
        agent than its registered role, and a mismatch is refused with a
        recorded ``policy_violation`` incident rather than created. A
        caller-supplied *model* must normalize to the role's exact policy pin;
        a missing, malformed, or differing model is refused, never substituted.
        Stage workers keep the existing direct-dispatch path, so this method is
        only used for supervised sessions.
        """
        bound_agent = agent_contracts_mod.role_agent(role)
        agent_contracts_mod.enforce_session_contract(
            self.ledger,
            self.job_id,
            policy=self.policy,
            role=role,
            observed_agent=bound_agent,
            requested_permissions=requested_permissions,
            requested_model=model,
            require_model=True,
            run_id=self.run_id,
        )
        return self.bridge.create_session(
            title=title, agent=bound_agent, model=model
        )

    def enforce_prompt_contract(
        self,
        action_id: int,
        *,
        role: str,
        observed_agent: str | None,
        requested_permissions: Sequence[str] | None = None,
        requested_model: Any = None,
        transport_config: Mapping[str, Any] | None = None,
        environ: Mapping[str, str] | None = None,
    ) -> agent_contracts_mod.TransportDecision:
        """Run the session contract and the pre-prompt transport gate.

        Order is deliberate: the identity/capability/model-pin contract first
        (a spoof, escalation, or unpinned model override is a
        ``policy_violation`` and must never reach the transport step), then the
        fail-closed egress gate, whose decision is recorded as action evidence
        **before** the caller writes the dispatch record. On a refusal nothing
        further is written and no prompt is issued.

        The isolated-transport case requires the *launched* service-owned
        session server's own fenceable identity, not merely a loopback
        address. The bridge derives that authority itself: it captures a
        :class:`LaunchedServerBinding` from the process identity the service
        launched and the transport the service created, and it refuses any
        caller-supplied transport override (a ``target``, ``server_address``,
        ``server_identity``, or ``server_binding``) rather than trusting it.
        """
        agent_contracts_mod.enforce_session_contract(
            self.ledger,
            self.job_id,
            policy=self.policy,
            role=role,
            observed_agent=observed_agent,
            requested_permissions=requested_permissions,
            requested_model=requested_model,
            require_model=True,
            run_id=self.run_id,
        )
        overrides = sorted(
            key
            for key in (transport_config or {})
            if key in agent_contracts_mod.CALLER_TRANSPORT_OVERRIDE_KEYS
        )
        if overrides:
            # A caller-supplied transport target/address/identity is not
            # authority: only the service-captured launched-server state may
            # authorize the isolated path. Refuse before any side effect and
            # record the refusal as the transport decision.
            decision = agent_contracts_mod.TransportDecision(
                enforced=False,
                path=agent_contracts_mod.TRANSPORT_UNENFORCED,
                detail=(
                    "caller-supplied transport override(s) "
                    f"{', '.join(overrides)} are not authority; the launched "
                    "service-server identity and address are service-owned and "
                    "immutable"
                ),
            )
            self.ledger.record_evidence(
                int(action_id),
                kind=agent_contracts_mod.EVIDENCE_TRANSPORT_DECISION,
                payload=decision.as_dict(),
            )
            raise agent_contracts_mod.EgressEnforcementError(
                f"pre-prompt transport enforcement failed: {decision.detail}",
                decision=decision,
            )
        # Service-owned binding, captured once at construction: the launched
        # server's identity plus the address of the transport the service
        # created. Neither is caller-overridable.
        config: dict[str, Any] = {}
        if self.server_binding is not None:
            config["server_binding"] = self.server_binding
        environment = os.environ if environ is None else environ
        return agent_contracts_mod.assert_pre_prompt_transport(
            self.ledger,
            action_id,
            environ=environment,
            config=config,
        )

    # -- prompt lifecycle ------------------------------------------------

    def prompt(
        self,
        session_id: str,
        *,
        text: str,
        stage: str = "primary",
        round_num: int | None = None,
        role: str = model_policy_mod.SUPERVISOR_ROLE,
        model: Mapping[str, Any] | None = None,
        agent: str | None = None,
        reserved_cost_usd: float | None = None,
        reserved_elapsed_minutes: float = 0.0,
        pricing_catalog_version: str | None = None,
        requested_permissions: Sequence[str] | None = None,
        transport_config: Mapping[str, Any] | None = None,
        environ: Mapping[str, str] | None = None,
    ) -> PromptIdentity:
        """Journal identity, reserve budget, dispatch, then prompt.

        At most one prompt per session is in flight: an existing in-flight (or
        unreconciled uncertain) prompt raises :class:`PromptInFlightError`
        before anything is written.

        Two fail-closed contracts run inside the intent window, before any
        server request: the session contract (the concrete agent the prompt
        runs under must be the role's registered agent, and no requested
        capability may exceed the role's allowlist) and the pre-prompt
        transport gate (model traffic must flow through the trusted gateway or
        an equivalently enforced isolated transport). Both fail the action —
        with the gate reason and, for the session contract, a recorded
        ``policy_violation`` incident — and issue no prompt.
        """
        existing = self.in_flight_prompt(session_id)
        if existing is not None:
            raise PromptInFlightError(
                f"action {existing['id']} is already in flight for session "
                f"{session_id!r}; reconcile it before prompting again"
            )
        bound_agent = agent or agent_contracts_mod.role_agent(role)
        request_id = new_request_id()
        detail = {
            "change_id": self.change_id,
            "stage": stage,
            "round": round_num,
            "role": role,
            "session_id": session_id,
            "request_id": request_id,
            "agent": bound_agent,
        }
        action_id = int(
            self.ledger.begin_action(
                self.job_id,
                kind=PROMPT_ACTION_KIND,
                run_id=self.run_id,
                detail=json.dumps(detail, sort_keys=True),
            )
        )
        try:
            self.enforce_prompt_contract(
                action_id,
                role=role,
                observed_agent=bound_agent,
                requested_permissions=requested_permissions,
                requested_model=model,
                transport_config=transport_config,
                environ=environ,
            )
        except agent_contracts_mod.AgentContractError as exc:
            # The contract refused before any server request: the action is
            # failed with the gate reason (the policy_violation incident is
            # already recorded) and no prompt is issued.
            self.ledger.fail_action(
                action_id, detail=f"{type(exc).__name__}: {exc}"
            )
            raise
        reservation_id: int | None = None
        if self.policy is not None and reserved_cost_usd is not None:
            reservation_id = int(
                budget_mod.reserve(
                    self.ledger,
                    job_id=self.job_id,
                    action_id=action_id,
                    role=role,
                    requested_model=_model_identity_string(model) or "",
                    reserved_cost_usd=float(reserved_cost_usd),
                    reserved_elapsed_minutes=float(reserved_elapsed_minutes),
                    policy=self.policy,
                    pricing_catalog_version=pricing_catalog_version,
                )
            )
        dispatch_id = int(
            self.ledger.dispatch_action(action_id, session_id=session_id)
        )
        if self.process_id:
            # The headless server's process identity rides on the same dispatch
            # row through the additive accessor (no schema change).
            self.ledger.bind_dispatch_identity(action_id, process_id=self.process_id)
        self.ledger.record_evidence(
            action_id,
            kind=EVIDENCE_SESSION_BINDING,
            payload={
                "session_id": session_id,
                "process_id": self.process_id,
                "request_id": request_id,
                "dispatch_id": dispatch_id,
            },
        )
        try:
            self.bridge.prompt_async(
                session_id,
                text=text,
                marker=request_id,
                model=model,
                agent=bound_agent,
            )
        except SessionBridgeError as exc:
            # The acknowledgement was lost or the request failed after the
            # identity became durable. Never re-prompt: mark the action
            # explicitly uncertain so lookup can reconcile it.
            self.ledger.record_evidence(
                action_id,
                kind=EVIDENCE_SPAWN_LOSS,
                payload={"outcome": "prompt_launch_uncertain", "confirmed": False,
                         "error": str(exc)},
            )
            self.ledger.mark_uncertain(action_id, detail=json.dumps(
                {**detail, "uncertainty": "prompt_launch_uncertain"}, sort_keys=True
            ))
            return PromptIdentity(
                request_id=request_id,
                action_id=action_id,
                session_id=session_id,
                reservation_id=reservation_id,
                acknowledged=False,
            )
        return PromptIdentity(
            request_id=request_id,
            action_id=action_id,
            session_id=session_id,
            reservation_id=reservation_id,
            acknowledged=True,
        )

    def recover_lost_ack(
        self,
        identity: PromptIdentity,
        *,
        sleep: Callable[[float], None] = time.sleep,
        max_attempts: int = budget_mod.BACKOFF_MAX_ATTEMPTS,
    ) -> dict[str, Any]:
        """Resolve an uncertain prompt launch by lookup, never by re-prompting.

        A still-pending observation is **not** a resolution: the original action
        is retained and re-observed until lookup yields a positive terminal
        result. The session's one-in-flight slot is never released while the
        prompt may still arrive — neither a marker-confirmed in-flight prompt
        nor an unobservable one permits a second prompt. The two are
        distinguished in evidence (``prompt_in_flight`` confirmed versus
        ``prompt_unobserved``), and the unknown reservation is retained rather
        than released, because the prompt's consumption is unresolved.
        """
        row = self.action_for_request(identity.request_id)
        if row is None:
            raise BridgeJournalError(
                f"no journaled action carries request identity "
                f"{identity.request_id!r}"
            )
        action_id = int(row["id"])
        result: dict[str, Any]
        try:
            result = poll_until_terminal(
                self.bridge, identity.session_id, marker=identity.request_id,
                sleep=sleep, max_attempts=max_attempts,
            )
        except SessionBridgeError:
            result = parse_result_schema(
                [], session_id=identity.session_id, marker=identity.request_id
            )
        marker_confirmed = bool(result.get("marker_found"))
        if not result["terminal"]:
            # Polling is still pending. This is an observation, not an outcome:
            # record whether the marker was actually discovered, keep the action
            # in the in-flight guard, and leave the reservation committed so no
            # second prompt can be issued against the session until a terminal
            # observation resolves the original one.
            self.ledger.record_evidence(
                action_id,
                kind=EVIDENCE_STAGE_RESULT,
                payload={
                    "outcome": (
                        "prompt_in_flight" if marker_confirmed else "prompt_unobserved"
                    ),
                    "confirmed": marker_confirmed,
                    "marker_found": marker_confirmed,
                },
            )
            if not marker_confirmed:
                # No observation at all: the prompt's consumption is unknown, so
                # the reservation is retained rather than left as a live
                # estimate. A marker-confirmed in-flight prompt keeps its
                # reservation so its eventual observed usage can be reconciled.
                self.retain_unknown_reservation(action_id)
            return {
                "reconciled": False,
                "terminal": False,
                "marker_confirmed": marker_confirmed,
                "duplicate_prompt_issued": False,
                "action_id": action_id,
                "result": result,
            }
        self.resolve(identity, result=result)
        return {
            "reconciled": True,
            "terminal": True,
            "marker_confirmed": marker_confirmed,
            "duplicate_prompt_issued": False,
            "action_id": action_id,
            "result": result,
        }

    def resolve(self, identity: PromptIdentity, *, result: Mapping[str, Any]) -> str:
        """Apply a terminal (or uncertain) result to the prompt's action."""
        action_id = int(identity.action_id)
        try:
            state = str(self.ledger.get_action(action_id)["state"])
        except Exception as exc:  # noqa: BLE001 - unknown action is a named failure
            raise BridgeJournalError(
                f"prompt action {action_id} is not journaled: {exc}"
            ) from exc
        if state in ("completed", "failed"):
            return state
        status = str(result.get("status") or "")
        if status == "pending":
            self.reconcile_prompt_reservation(action_id, result)
            if state in ("intent", "dispatched"):
                self.ledger.record_evidence(
                    action_id,
                    kind=EVIDENCE_STAGE_RESULT,
                    payload={"outcome": "prompt_unresolved", "confirmed": False},
                )
                self.ledger.mark_uncertain(
                    action_id,
                    detail=json.dumps(
                        {"uncertainty": "prompt_unresolved", "request_id": identity.request_id},
                        sort_keys=True,
                    ),
                )
                return "uncertain"
            # An already uncertain or reconciled in-flight prompt stays in the
            # guard: a pending observation is not a resolution.
            return "uncertain"
        self.reconcile_prompt_reservation(action_id, result)
        usage = result.get("usage")
        self.ledger.record_evidence(
            action_id,
            kind=EVIDENCE_STAGE_RESULT,
            payload={
                "outcome": status,
                "confirmed": status == "completed",
                "usage": usage,
                "cost": result.get("cost"),
                "marker": result.get("marker"),
            },
        )
        if isinstance(usage, Mapping) and usage.get("usage_available"):
            # The observed usage is journaled in the same evidence kind the
            # stage dispatch path uses, so a duplicate or replayed observation
            # deduplicates at the reservation boundary and is never double-billed.
            self.ledger.record_evidence(
                action_id, kind=EVIDENCE_USAGE, payload=dict(usage)
            )
        if status == "completed":
            if state == "uncertain":
                self.ledger.reconcile_action(action_id)
            self.ledger.complete_action(action_id)
            return "completed"
        if state == "uncertain":
            self.ledger.reconcile_action(action_id)
        self.ledger.fail_action(action_id, detail=status)
        return "failed"

    def reconcile_prompt_reservation(
        self, action_id: int, result: Mapping[str, Any]
    ) -> str | None:
        """Reconcile observed usage once; a replayed observation is a no-op."""
        reservation = self.ledger.reservation_for_action(int(action_id))
        if reservation is None:
            return None
        if reservation["state"] != "reserved":
            return str(reservation["state"])
        usage = result.get("usage") if isinstance(result.get("usage"), Mapping) else {}
        cost = result.get("cost") if isinstance(result.get("cost"), Mapping) else {}
        status = str(result.get("status") or "")
        if status in ("completed",) and usage.get("usage_available") and cost.get("status") == "estimated":
            observation_state = "observed"
        elif status in ("error", "aborted") and usage.get("usage_available"):
            observation_state = "observed"
        else:
            observation_state = "unknown"
        return budget_mod.reconcile(
            self.ledger,
            reservation_id=int(reservation["id"]),
            observation_state=observation_state,
            observed_input_tokens=usage.get("input_tokens"),
            observed_output_tokens=usage.get("output_tokens"),
            observed_cached_tokens=usage.get("cached_input_tokens"),
            observed_reasoning_tokens=usage.get("reasoning_tokens"),
            observed_cost_usd=cost.get("estimated_cost"),
            observed_elapsed_minutes=(
                float(result["duration_ms"]) / 60000.0
                if isinstance(result.get("duration_ms"), (int, float))
                else None
            ),
        )

    def retain_unknown_reservation(self, action_id: int) -> bool:
        """Retain an unresolved reservation rather than releasing it."""
        reservation = self.ledger.reservation_for_action(int(action_id))
        if reservation is None:
            return False
        self.ledger.retain_reservation(int(reservation["id"]))
        return True

    # -- hints -----------------------------------------------------------

    def observe_hint(self, hint: SessionEventHint) -> dict[str, Any]:
        """Reconcile a hint against the journal; a hint is never an outcome.

        A hint matching a journaled action is recorded at most as evidence, and
        a repeated message/part identity is recognized against the evidence
        already recorded for that dispatch, so a duplicate delivery adds
        nothing. An orphan hint (no matching action) is reported as an
        observation only and never recorded as an action outcome.
        """
        row = None
        if hint.message_id:
            for candidate in self.ledger.list_actions(self.job_id):
                if candidate["kind"] != PROMPT_ACTION_KIND:
                    continue
                detail = _decode_detail(candidate["detail"])
                if detail.get("session_id") not in (None, hint.session_id):
                    continue
                if candidate["state"] in IN_FLIGHT_ACTION_STATES:
                    row = candidate
                    break
        if row is None:
            return {
                "matched_action_id": None, "recorded": False,
                "orphan": True, "duplicate": False,
            }
        identity = hint.identity
        for existing in self.ledger.list_evidence(int(row["id"])):
            if existing["kind"] != EVIDENCE_HINT:
                continue
            payload = _decode_detail(existing["payload"])
            if identity is not None and payload.get("hint_identity") == list(identity):
                return {
                    "matched_action_id": int(row["id"]),
                    "recorded": False,
                    "orphan": False,
                    "duplicate": True,
                }
        payload = {
            "hint_id": hint.event_id,
            "hint_identity": list(identity) if identity is not None else None,
            "kind": hint.kind,
            "message_id": hint.message_id,
            "part_id": hint.part_id,
        }
        self.ledger.record_evidence(
            int(row["id"]), kind=EVIDENCE_HINT, payload=payload
        )
        return {
            "matched_action_id": int(row["id"]), "recorded": True,
            "orphan": False, "duplicate": False,
        }


def _decode_detail(value: Any) -> dict[str, Any]:
    if isinstance(value, Mapping):
        return dict(value)
    if not isinstance(value, str) or not value:
        return {}
    try:
        decoded = json.loads(value)
    except (TypeError, ValueError):
        return {}
    return dict(decoded) if isinstance(decoded, Mapping) else {}


def _model_identity_string(model: Mapping[str, Any] | None) -> str | None:
    if not isinstance(model, Mapping):
        return None
    provider = model.get("providerID")
    model_id = model.get("modelID")
    if isinstance(provider, str) and isinstance(model_id, str):
        return f"{provider}/{model_id}"
    return None


# ---------------------------------------------------------------------------
# Primary session linkage (journaled, additive — no schema migration)
# ---------------------------------------------------------------------------


def record_primary_session_linkage(
    ledger: Any,
    job_id: int,
    *,
    run_id: str,
    server_address: str,
    session_id: str,
    process_id: str | None = None,
) -> PrimarySessionLinkage:
    """Record the primary session linkage as a journaled, additive record.

    No ledger schema change is involved: the linkage rides in the existing
    action detail and ``session_binding`` evidence of a dedicated linkage
    action, which is the same durable surface the dispatch identity uses.
    """
    detail = {
        "server_address": server_address,
        "session_id": session_id,
        "process_id": process_id,
    }
    action_id = int(
        ledger.begin_action(
            int(job_id),
            kind=SESSION_LINKAGE_ACTION_KIND,
            run_id=str(run_id),
            detail=json.dumps(detail, sort_keys=True),
        )
    )
    ledger.dispatch_action(action_id, session_id=session_id, process_id=process_id)
    ledger.record_evidence(
        action_id, kind=EVIDENCE_SESSION_BINDING, payload=dict(detail)
    )
    ledger.complete_action(action_id)
    return PrimarySessionLinkage(
        job_id=int(job_id),
        server_address=str(server_address),
        session_id=str(session_id),
        process_id=process_id,
        recorded_at=clock_mod.utcnow(),
    )


def primary_session_linkage(ledger: Any, job_id: int) -> PrimarySessionLinkage | None:
    """Return the job's most recently recorded primary session linkage."""
    latest: PrimarySessionLinkage | None = None
    for row in ledger.list_actions(int(job_id)):
        if row["kind"] != SESSION_LINKAGE_ACTION_KIND:
            continue
        detail = _decode_detail(row["detail"])
        server_address = detail.get("server_address")
        session_id = detail.get("session_id")
        if not isinstance(server_address, str) or not isinstance(session_id, str):
            continue
        latest = PrimarySessionLinkage(
            job_id=int(job_id),
            server_address=server_address,
            session_id=session_id,
            process_id=detail.get("process_id"),
            recorded_at=str(row["intent_at"] or ""),
        )
    return latest


def attach_target(ledger: Any, job_id: int) -> PrimarySessionLinkage | None:
    """Return the linkage an operator's interactive chat starts or attaches to.

    The recorded primary session linkage is the single authoritative target: a
    chat that starts or attaches through it joins the same service-managed
    session instead of a divergent private one.
    """
    return primary_session_linkage(ledger, job_id)


# ---------------------------------------------------------------------------
# Bounded briefing composer
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class BriefingSection:
    """One named, ordered briefing section (oldest line first)."""

    name: str
    blocking: bool
    lines: tuple[str, ...]

    def render(self) -> str:
        header = f"[{self.name}]{' (blocking)' if self.blocking else ''}"
        return "\n".join([header, *self.lines])


@dataclass(frozen=True)
class Briefing:
    """A bounded briefing composed from durable state, never a transcript."""

    mode: str
    sections: tuple[BriefingSection, ...]
    bound: int
    truncated: bool = False

    @property
    def blocking_sections(self) -> tuple[BriefingSection, ...]:
        return tuple(section for section in self.sections if section.blocking)

    def render(self) -> str:
        header = (
            f"# session briefing ({self.mode}, schema {BRIEFING_SCHEMA_VERSION})"
        )
        body = "\n\n".join(section.render() for section in self.sections)
        return f"{header}\n\n{body}\n"

    def as_dict(self) -> dict[str, Any]:
        return {
            "version": BRIEFING_SCHEMA_VERSION,
            "mode": self.mode,
            "bound": self.bound,
            "truncated": self.truncated,
            "sections": [
                {
                    "name": section.name,
                    "blocking": section.blocking,
                    "lines": list(section.lines),
                }
                for section in self.sections
            ],
        }

    def text(self) -> str:
        return self.render()


def bound_sections(
    sections: Sequence[BriefingSection], bound: int
) -> tuple[tuple[BriefingSection, ...], bool]:
    """Apply the documented bounding rule and report whether anything was shed.

    Blocking items are always retained in full; overflow is shed from the
    oldest non-blocking detail first. If only blocking content remains and it
    still exceeds the bound, it is retained anyway — an uncertain action or a
    pending gate is never summarized away.
    """
    working = [
        {"name": s.name, "blocking": s.blocking, "lines": list(s.lines)}
        for s in sections
    ]

    def total() -> int:
        return sum(
            len(s["lines"]) + (1 if s["lines"] else 0) + len(s["name"]) + 4
            for s in working
        )

    truncated = False
    while total() > bound:
        victim = next(
            (s for s in reversed(working) if not s["blocking"] and s["lines"]),
            None,
        )
        if victim is None:
            break
        victim["lines"].pop(0)
        truncated = True
    return (
        tuple(
            BriefingSection(name=s["name"], blocking=s["blocking"], lines=tuple(s["lines"]))
            for s in working
        ),
        truncated,
    )


def compose_briefing(
    ledger: Any,
    job_id: int,
    *,
    mode: str = "full",
    change_id: str | None = None,
    bound: int | None = None,
    authority: Mapping[str, Any] | None = None,
) -> Briefing:
    """Compose a bounded briefing from the ledger and the authority store.

    Sources: the journaled job record and protected plan snapshot reference,
    active and unreconciled uncertain actions (rendered as blocking state),
    budget and reservation state, incident history including previous failed
    remedies, and — when *change_id* is supplied — the broker's gate resolution.
    A transcript is never a source. ``mode`` is ``"full"`` for a replacement
    session and ``"rebrief"`` for an adopted session, which receives the
    smaller :data:`REBRIEF_BOUND_CHARS` bound.
    """
    if mode not in ("full", "rebrief"):
        raise BridgeJournalError(f"unknown briefing mode {mode!r}")
    effective_bound = bound if bound is not None else (
        BRIEFING_BOUND_CHARS if mode == "full" else REBRIEF_BOUND_CHARS
    )
    sections: list[BriefingSection] = []
    job = ledger.get_job(int(job_id))
    sections.append(
        BriefingSection(
            name="job",
            blocking=False,
            lines=(
                f"job {job['id']} state={job['state']} run={job['run_id']}",
                f"worktree={job['worktree_path']} owner={job['owner']}",
            ),
        )
    )
    snapshot_hash = None
    try:
        policy = ledger.current_policy(int(job_id))
        snapshot_hash = policy.get("manifest_snapshot_hash")
    except Exception:  # noqa: BLE001 - a missing policy is durable-state absence
        policy = {}
    sections.append(
        BriefingSection(
            name="plan_snapshot",
            blocking=False,
            lines=(f"protected manifest snapshot {snapshot_hash or '(none recorded)'}",),
        )
    )

    uncertain_rows = [row for row in ledger.list_uncertain_actions(int(job_id))]
    active_rows = [
        row
        for row in ledger.list_actions(int(job_id))
        if row["state"] in ("intent", "dispatched")
    ]
    blocking_lines: list[str] = []
    for row in uncertain_rows:
        detail = _decode_detail(row["detail"])
        blocking_lines.append(
            f"UNCERTAIN action {row['id']} kind={row['kind']} "
            f"request={detail.get('request_id') or '?'} "
            f"uncertainty={detail.get('uncertainty') or 'unrecorded'}: reconcile "
            "from evidence before progressing"
        )
    for row in active_rows:
        blocking_lines.append(
            f"ACTIVE action {row['id']} kind={row['kind']} state={row['state']}"
        )
    if change_id:
        gate_line = _gate_line(ledger, int(job_id), change_id, authority)
        if gate_line:
            blocking_lines.append(gate_line)
    sections.append(
        BriefingSection(
            name="blocking",
            blocking=True,
            lines=tuple(blocking_lines) or ("no blocking items",),
        )
    )

    reservations = [dict(row) for row in ledger.reservations_for_job(int(job_id))]
    consumption = budget_mod.sum_reservations(reservations)
    sections.append(
        BriefingSection(
            name="budget",
            blocking=False,
            lines=(
                f"cost_usd={consumption['cost_usd']:.6f} "
                f"reserved={consumption['reserved_cost_usd']:.6f} "
                f"retained={consumption['retained_cost_usd']:.6f} "
                f"reconciled={consumption['reconciled_cost_usd']:.6f}",
                f"elapsed_minutes={consumption['elapsed_minutes']:.6f} "
                f"reservations={consumption['reservation_count']}",
            ),
        )
    )

    incident_lines: list[str] = []
    failed_remedy_lines: list[str] = []
    for row in ledger.list_incidents(int(job_id)):
        incident_lines.append(
            f"incident {row['id']} kind={row['kind']} state={row['state']} "
            f"summary={row['summary'] or '(none)'}"
        )
        attempts = ledger.incident_attempt_count(int(job_id), str(row["kind"]))
        if attempts:
            failed_remedy_lines.append(
                f"remedy for incident {row['id']} kind={row['kind']} attempted "
                f"{attempts} time(s) without resolution"
            )
    sections.append(
        BriefingSection(
            name="incidents",
            blocking=False,
            lines=tuple(incident_lines) or ("no recorded incidents",),
        )
    )
    sections.append(
        BriefingSection(
            name="failed_remedies",
            blocking=False,
            lines=tuple(failed_remedy_lines) or ("no failed remedies recorded",),
        )
    )
    bounded, truncated = bound_sections(sections, int(effective_bound))
    return Briefing(
        mode=mode, sections=bounded, bound=int(effective_bound), truncated=truncated
    )


def _gate_line(
    ledger: Any,
    job_id: int,
    change_id: str,
    authority: Mapping[str, Any] | None,
) -> str | None:
    if isinstance(authority, Mapping):
        dispatchable = authority.get("dispatchable")
        reason = authority.get("reason")
        return (
            f"GATE change={change_id} dispatchable={bool(dispatchable)} "
            f"reason={reason or 'unrecorded'}"
        )
    try:
        resolution = broker_mod.resolve_gate(ledger, job_id, change_id)
    except broker_mod.BrokerError:
        return f"GATE change={change_id} authority state unavailable"
    if resolution.dispatchable:
        return None
    return (
        f"GATE change={change_id} dispatchable=False reason={resolution.reason}"
    )


__all__ = [
    "BRIEFING_BOUND_CHARS",
    "BRIEFING_SCHEMA_VERSION",
    "BridgeJournalError",
    "Briefing",
    "BriefingSection",
    "CapabilityNotCheckedError",
    "DOCUMENTED_API_SUBSET",
    "EVIDENCE_HINT",
    "EVIDENCE_SESSION_BINDING",
    "EVIDENCE_SPAWN_LOSS",
    "EVIDENCE_STAGE_RESULT",
    "EVIDENCE_USAGE",
    "EVENT_STREAM_MEDIA_TYPE",
    "EVENT_STREAM_PATH",
    "HEALTH_PATH",
    "HTTPResponse",
    "HintConsumer",
    "IN_FLIGHT_ACTION_STATES",
    "JournaledSessionBridge",
    "LoopbackTransport",
    "PROMPT_ACTION_KIND",
    "PrimarySessionLinkage",
    "PromptIdentity",
    "PromptInFlightError",
    "REBRIEF_BOUND_CHARS",
    "REQUEST_MARKER_PREFIX",
    "RESULT_SCHEMA_NAME",
    "RESULT_SCHEMA_VERSION",
    "SESSION_LINKAGE_ACTION_KIND",
    "SUPPORTED_SERVER_VERSION_MAX",
    "SUPPORTED_SERVER_VERSION_MIN",
    "SUPPORTED_SERVER_VERSION_RANGE",
    "SessionBridge",
    "SessionBridgeError",
    "SessionCapability",
    "SessionEventHint",
    "SessionNotFoundError",
    "TransportError",
    "UnreachableServerError",
    "UnsupportedVersionError",
    "attach_target",
    "bound_sections",
    "compose_briefing",
    "extract_marker",
    "find_marker",
    "is_loopback_host",
    "new_request_id",
    "parse_event_frame",
    "parse_event_stream",
    "parse_loopback_address",
    "parse_model_identity",
    "parse_process_identity",
    "parse_result_schema",
    "parse_server_version",
    "poll_until_terminal",
    "primary_session_linkage",
    "record_primary_session_linkage",
    "request_marker",
    "result_cost",
    "result_usage",
    "serialize_process_identity",
    "version_in_supported_range",
]
