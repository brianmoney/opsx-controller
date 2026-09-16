"""Contract tests for the OpenCode session bridge over the action journal.

Everything runs against a loopback fake OpenCode server (stdlib
``http.server`` in a thread) implementing exactly the documented API subset,
plus fault knobs: version mismatch, accept-then-drop-ack, duplicate and
out-of-order events, stream disconnect without replay, and slow terminal
state. Real local subprocesses stand in for the headless server where process
identity matters. Model inputs are faked. No external network, no paid model.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from unittest import mock

from lib.supervisor import agent_contracts as agent_contracts_mod
from lib.supervisor import budgets as budget_mod
from lib.supervisor import ledger, lock as lock_mod, model_policy
from lib.supervisor import session_bridge as bridge_mod

REPO_ROOT = Path(__file__).resolve().parents[2]

MANIFEST = (
    "[[changes]]\n"
    'id = "add-opencode-session-bridge"\n'
    "pause_before = false\n"
    "depends_on = []\n"
)

SUPPORTED_VERSION = "1.18.31"


# ---------------------------------------------------------------------------
# Loopback fake OpenCode server
# ---------------------------------------------------------------------------


class FakeOpencodeState:
    """Server-side state for the fake, with fault-injection knobs."""

    def __init__(self, *, version: str = SUPPORTED_VERSION) -> None:
        self.version = version
        self.sessions: dict[str, dict] = {}
        self.messages: dict[str, list[dict]] = {}
        self.status: dict[str, str] = {}
        self.prompt_calls: list[dict] = []
        self.requests: list[tuple[str, str]] = []
        self.aborts: list[str] = []
        self.drop_ack_once = False
        self.hold_prompt_record = False
        self.held_prompts: dict[str, dict] = {}
        self.slow = False
        self.slow_delay = 0.25
        self._counter = 0

    def next_id(self, prefix: str) -> str:
        self._counter += 1
        return f"{prefix}_{self._counter:04d}"

    def release_held_prompt(self, session_id: str) -> bool:
        """Deliver a prompt whose server-side record was withheld (delayed)."""
        held = self.held_prompts.pop(session_id, None)
        if held is None:
            return False
        _record_prompt_answer(self, session_id, held)
        return True

    def seed_completed_turn(self, session_id: str, *, marker: str) -> str:
        """Seed an earlier request's user message plus its completed reply."""
        user_id = self.next_id("msg")
        self.messages[session_id].append(
            {
                "info": {
                    "id": user_id,
                    "sessionID": session_id,
                    "role": "user",
                    "time": {"created": int(time.time() * 1000) - 10_000},
                    "agent": "supervisor",
                    "model": {"providerID": "fake", "modelID": "fake"},
                },
                "parts": [
                    {
                        "id": self.next_id("prt"),
                        "sessionID": session_id,
                        "messageID": user_id,
                        "type": "text",
                        "text": f"earlier request\n\n{marker}",
                    }
                ],
            }
        )
        _complete_turn(self, session_id, user_id)
        return user_id

    def complete_pending(self, session_id: str) -> bool:
        """Complete the session's outstanding turn without a new prompt."""
        for entry in self.messages.get(session_id, []):
            info = entry.get("info") or {}
            if info.get("role") == "assistant":
                return False
        user = next(
            (
                entry
                for entry in self.messages.get(session_id, [])
                if (entry.get("info") or {}).get("role") == "user"
            ),
            None,
        )
        if user is None:
            return False
        _complete_turn(self, session_id, str((user.get("info") or {}).get("id") or ""))
        return True


def _prompt_text(body: dict) -> str:
    parts = body.get("parts") or []
    return " ".join(
        part.get("text", "") for part in parts if isinstance(part, dict)
    )


def _complete_turn(state: FakeOpencodeState, session_id: str, user_id: str) -> None:
    assistant_id = state.next_id("msg")
    started = int(time.time() * 1000)
    state.messages[session_id].append(
        {
            "info": {
                "id": assistant_id,
                "sessionID": session_id,
                "role": "assistant",
                "parentID": user_id,
                "time": {"created": started, "completed": started + 12},
                "providerID": "fake",
                "modelID": "fake",
                "cost": 0.25,
                "tokens": {
                    "total": 30,
                    "input": 20,
                    "output": 10,
                    "reasoning": 0,
                    "cache": {"read": 0, "write": 0},
                },
            },
            "parts": [
                {
                    "id": state.next_id("prt"),
                    "sessionID": session_id,
                    "messageID": assistant_id,
                    "type": "text",
                    "text": "ack",
                }
            ],
        }
    )
    state.status[session_id] = "idle"


def _record_prompt_answer(
    state: FakeOpencodeState, session_id: str, body: dict
) -> str:
    """Record the prompt's user message and (usually) its completed reply."""
    marker = bridge_mod.extract_marker(_prompt_text(body))
    user_id = state.next_id("msg")
    state.messages[session_id].append(
        {
            "info": {
                "id": user_id,
                "sessionID": session_id,
                "role": "user",
                "time": {"created": int(time.time() * 1000)},
                "agent": "supervisor",
                "model": body.get("model") or {"providerID": "fake", "modelID": "fake"},
            },
            "parts": [
                {
                    "id": state.next_id("prt"),
                    "sessionID": session_id,
                    "messageID": user_id,
                    "type": "text",
                    "text": _prompt_text(body),
                }
            ],
        }
    )
    if os.environ.get("FAKE_PROMPT_NEVER_COMPLETES") == "1":
        state.status[session_id] = "busy"
        return user_id
    _complete_turn(state, session_id, user_id)
    return user_id


class _Handler(BaseHTTPRequestHandler):
    state: FakeOpencodeState
    protocol_version = "HTTP/1.1"

    def log_message(self, *args):  # noqa: D102 - silence test server logging
        return

    # -- helpers ---------------------------------------------------------

    def _read_body(self) -> dict:
        length = int(self.headers.get("content-length") or 0)
        if not length:
            return {}
        raw = self.rfile.read(length)
        try:
            decoded = json.loads(raw.decode("utf-8"))
        except (TypeError, ValueError):
            return {}
        return decoded if isinstance(decoded, dict) else {}

    def _send(self, status: int, payload) -> None:
        body = b"" if payload is None else json.dumps(payload).encode("utf-8")
        self.send_response(status)
        if body:
            self.send_header("content-type", "application/json")
        self.send_header("content-length", str(len(body)))
        self.end_headers()
        if body:
            self.wfile.write(body)

    def _prompt_text(self, body: dict) -> str:
        return _prompt_text(body)

    def _answer_prompt(self, session_id: str, body: dict) -> str:
        return _record_prompt_answer(self.state, session_id, body)

    # -- documented API subset -------------------------------------------

    def do_GET(self):  # noqa: N802 - stdlib handler API
        path = self.path.split("?", 1)[0]
        self.state.requests.append(("GET", path))
        if path == "/global/health":
            self._send(200, {"healthy": True, "version": self.state.version})
            return
        if path == "/session":
            self._send(200, list(self.state.sessions.values()))
            return
        if path.endswith("/message") and path.startswith("/session/"):
            session_id = path.split("/")[2]
            if session_id not in self.state.sessions:
                self._send(404, {"error": "not found"})
                return
            self._send(200, self.state.messages.get(session_id, []))
            return
        if path.startswith("/session/") and path.count("/") == 2:
            session_id = path.split("/")[2]
            session = self.state.sessions.get(session_id)
            if session is None:
                self._send(404, {"error": "not found"})
                return
            self._send(200, session)
            return
        if path == "/event":
            self._serve_event_stream()
            return
        self._send(404, {"error": "not found"})

    def do_POST(self):  # noqa: N802 - stdlib handler API
        path = self.path.split("?", 1)[0]
        self.state.requests.append(("POST", path))
        body = self._read_body()
        if path == "/session":
            session_id = self.state.next_id("ses")
            session = {
                "id": session_id,
                "title": body.get("title") or "untitled",
                "directory": "/tmp/fake",
                "cost": 0.0,
                "tokens": {
                    "input": 0,
                    "output": 0,
                    "reasoning": 0,
                    "cache": {"read": 0, "write": 0},
                },
                "time": {"created": int(time.time() * 1000)},
            }
            self.state.sessions[session_id] = session
            self.state.messages[session_id] = []
            self._send(200, session)
            return
        if path.endswith("/prompt_async") and path.startswith("/session/"):
            session_id = path.split("/")[2]
            if session_id not in self.state.sessions:
                self._send(404, {"error": "not found"})
                return
            self.state.prompt_calls.append({"session_id": session_id, "body": body})
            if self.state.hold_prompt_record:
                # Accept the prompt but withhold any discoverable record, so
                # lookup cannot yet find the marker (undiscoverable/delayed ack).
                self.state.held_prompts[session_id] = dict(body)
            else:
                self._answer_prompt(session_id, body)
            if self.state.drop_ack_once:
                self.state.drop_ack_once = False
                # Accept the prompt server-side, then drop the acknowledgement.
                self.close_connection = True
                try:
                    self.connection.close()
                except OSError:
                    pass
                return
            self._send(200, {})
            return
        if path.endswith("/abort") and path.startswith("/session/"):
            session_id = path.split("/")[2]
            self.state.aborts.append(session_id)
            if session_id not in self.state.sessions:
                self._send(404, {"error": "not found"})
                return
            self._send(200, True)
            return
        if path.endswith("/message") and path.startswith("/session/"):
            session_id = path.split("/")[2]
            if session_id not in self.state.sessions:
                self._send(404, {"error": "not found"})
                return
            self.state.prompt_calls.append({"session_id": session_id, "body": body})
            self._answer_prompt(session_id, body)
            self._send(200, {})
            return
        self._send(404, {"error": "not found"})

    # -- SSE -------------------------------------------------------------

    def _serve_event_stream(self) -> None:
        self.send_response(200)
        self.send_header("content-type", "text/event-stream")
        self.send_header("cache-control", "no-cache")
        self.send_header("connection", "close")
        self.end_headers()
        script = json.loads(os.environ.get("FAKE_EVENT_SCRIPT") or "[]")
        try:
            for frame in script:
                self.wfile.write(
                    ("data: " + json.dumps(frame) + "\n\n").encode("utf-8")
                )
                self.wfile.flush()
            if os.environ.get("FAKE_EVENT_DISCONNECT") == "1":
                self.close_connection = True
                self.connection.close()
                return
            deadline = time.time() + 5
            while time.time() < deadline:
                time.sleep(0.05)
        except (BrokenPipeError, ConnectionResetError, OSError):
            return


class FakeOpencodeServer:
    """A threaded loopback fake server for the documented API subset."""

    def __init__(self, *, version: str = SUPPORTED_VERSION) -> None:
        self.state = FakeOpencodeState(version=version)
        handler = type("_BoundHandler", (_Handler,), {"state": self.state})
        self._server = ThreadingHTTPServer(("127.0.0.1", 0), handler)
        self._thread = threading.Thread(target=self._server.serve_forever, daemon=True)

    @property
    def address(self) -> str:
        host, port = self._server.server_address[:2]
        return f"{host}:{port}"

    def __enter__(self) -> "FakeOpencodeServer":
        self._thread.start()
        return self

    def __exit__(self, *exc) -> None:
        self._server.shutdown()
        self._server.server_close()


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


def _selection() -> dict:
    return {
        "version": model_policy.MODEL_POLICY_VERSION,
        "roles": {role: "openai/gpt-4o" for role in model_policy.POLICY_ROLES},
        "stages": dict(model_policy.STANDARD_STAGE_MAPPING),
    }


def _policy(*, total_cost_usd: float = 100.0) -> dict:
    return {
        "authority_config": {"mode": "policy-bound"},
        "model_selection": _selection(),
        "inexpensive_allowlist": {
            "version": model_policy.MODEL_POLICY_VERSION,
            "models": ["openai/gpt-4o"],
            "source": "test fixture",
        },
        "manifest_snapshot_hash": "deadbeef",
        "budgets": {
            "version": budget_mod.BUDGET_SCHEMA_VERSION,
            "total_cost_usd": total_cost_usd,
            "per_action_cost_usd": None,
            "total_elapsed_minutes": None,
            "per_action_elapsed_minutes": None,
            "max_incident_attempts": None,
        },
        "deadlines": {
            "version": budget_mod.BUDGET_SCHEMA_VERSION,
            "execution_deadline_minutes": None,
        },
    }


class BridgeTestCase(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.repo = self.root / "repo"
        self.repo.mkdir()
        subprocess.run(["git", "init"], cwd=self.repo, check=True, capture_output=True)
        self.storage = self.root / "service-storage"
        self.storage.mkdir()
        self.db_path = self.storage / "supervisor.sqlite3"
        self.ledger = ledger.open_ledger(self.db_path, repository_root=self.repo)
        self.addCleanup(self.ledger.close)
        self.job_id = self.ledger.register_job(
            run_id="run-1",
            worktree=self.repo,
            owner="service",
            policy=_policy(),
            operator="operator",
            manifest_content=MANIFEST,
        )
        self.policy = self.ledger.current_policy(self.job_id)

    def make_bridge(self, server: FakeOpencodeServer, *, check: bool = True):
        transport = bridge_mod.LoopbackTransport.from_address(server.address)
        self.addCleanup(transport.close)
        bridge = bridge_mod.SessionBridge(transport)
        if check:
            bridge.check_capability()
        return bridge

    def journaled(self, server: FakeOpencodeServer, *, process_id: str | None = None):
        bridge = self.make_bridge(server)
        # The loopback fake server runs in this process, so this process's
        # fenceable identity *is* the launched service-server identity the
        # isolated-transport decision requires. A test that supplies an
        # explicit process_id overrides it, and a test that wants the
        # unenforced path uses a non-loopback transport.
        server_identity = process_id or bridge_mod.serialize_process_identity(os.getpid())
        if process_id is None:
            process_id = server_identity
        return bridge_mod.JournaledSessionBridge(
            bridge,
            self.ledger,
            job_id=self.job_id,
            run_id="run-1",
            policy=self.policy,
            process_id=process_id,
            server_identity=server_identity,
            change_id="add-opencode-session-bridge",
        )


# ---------------------------------------------------------------------------
# Documented API and version capability check
# ---------------------------------------------------------------------------


class DocumentedApiTests(BridgeTestCase):
    def test_documented_subset_is_the_only_surface_used(self) -> None:
        with FakeOpencodeServer() as server:
            bridge = self.make_bridge(server)
            session = bridge.create_session(title="t")
            bridge.prompt_async(session["id"], text="hello", marker="req_x")
            bridge.lookup_session(session["id"])
            bridge.lookup_messages(session["id"])
            bridge.abort(session["id"])
            bridge.transport.close()
        used = set(server.state.requests)
        allowed = {
            ("GET", "/global/health"),
            ("POST", "/session"),
            ("GET", f"/session/{session['id']}"),
            ("GET", f"/session/{session['id']}/message"),
            ("POST", f"/session/{session['id']}/prompt_async"),
            ("POST", f"/session/{session['id']}/abort"),
        }
        self.assertTrue(used.issubset(allowed), used - allowed)

    def test_supported_range_gating_and_named_failure(self) -> None:
        with FakeOpencodeServer(version="1.19.5") as server:
            bridge = self.make_bridge(server, check=False)
            with self.assertRaises(bridge_mod.UnsupportedVersionError) as ctx:
                bridge.check_capability()
            self.assertIn(bridge_mod.SUPPORTED_SERVER_VERSION_RANGE, str(ctx.exception))
            # No session operation was issued at all.
            self.assertEqual(
                [r for r in server.state.requests if r[1] != "/global/health"], []
            )

    def test_refusal_before_a_successful_check(self) -> None:
        with FakeOpencodeServer() as server:
            bridge = self.make_bridge(server, check=False)
            calls = (
                (lambda: bridge.create_session(title="x")),
                (lambda: bridge.lookup_session("ses_1")),
                (lambda: bridge.lookup_messages("ses_1")),
                (lambda: bridge.abort("ses_1")),
                (lambda: bridge.prompt_async("ses_1", text="hi")),
                (lambda: bridge.result("ses_1")),
                (lambda: bridge.open_event_stream()),
            )
            for call in calls:
                with self.assertRaises(bridge_mod.CapabilityNotCheckedError):
                    call()
            self.assertEqual(server.state.requests, [])

    def test_unreachable_server_fails_closed(self) -> None:
        transport = bridge_mod.LoopbackTransport("127.0.0.1", 1, timeout=0.5)
        self.addCleanup(transport.close)
        bridge = bridge_mod.SessionBridge(transport)
        with self.assertRaises(bridge_mod.UnreachableServerError):
            bridge.check_capability()

    def test_non_loopback_address_refused(self) -> None:
        with self.assertRaises(bridge_mod.TransportError):
            bridge_mod.LoopbackTransport("10.0.0.5", 4096)
        with self.assertRaises(bridge_mod.TransportError):
            bridge_mod.parse_loopback_address("example.com:4096")

    def test_malformed_version_is_unsupported(self) -> None:
        self.assertIsNone(bridge_mod.parse_server_version("not-a-version"))
        self.assertIsNone(bridge_mod.parse_server_version(None))
        self.assertEqual(bridge_mod.parse_server_version("1.18"), (1, 18, 0))

    def test_prompt_result_follows_result_schema(self) -> None:
        with FakeOpencodeServer() as server:
            bridge = self.make_bridge(server)
            session = bridge.create_session(title="schema")
            bridge.prompt_async(
                session["id"], text="do it", marker="req_schema"
            )
            result = bridge.result(session["id"], marker="req_schema")
        self.assertEqual(result["version"], bridge_mod.RESULT_SCHEMA_VERSION)
        self.assertEqual(result["schema"], bridge_mod.RESULT_SCHEMA_NAME)
        self.assertTrue(result["marker_found"])
        self.assertEqual(result["status"], "completed")
        self.assertTrue(result["terminal"])
        self.assertEqual(result["source"], "poll")
        self.assertEqual(result["usage"]["input_tokens"], 20)
        self.assertEqual(result["usage"]["output_tokens"], 10)
        self.assertEqual(result["cost"]["status"], "estimated")
        self.assertEqual(result["cost"]["estimated_cost"], 0.25)

    def test_slow_terminal_state_is_pending_until_completed(self) -> None:
        old = os.environ.get("FAKE_PROMPT_NEVER_COMPLETES")
        os.environ["FAKE_PROMPT_NEVER_COMPLETES"] = "1"
        try:
            with FakeOpencodeServer() as server:
                bridge = self.make_bridge(server)
                session = bridge.create_session(title="slow")
                bridge.prompt_async(session["id"], text="slow", marker="req_slow")
                result = bridge.result(session["id"], marker="req_slow")
        finally:
            if old is None:
                os.environ.pop("FAKE_PROMPT_NEVER_COMPLETES", None)
            else:
                os.environ["FAKE_PROMPT_NEVER_COMPLETES"] = old
        self.assertEqual(result["status"], "pending")
        self.assertFalse(result["terminal"])

    def test_poll_loop_is_bounded_and_returns_the_pending_observation(self) -> None:
        old = os.environ.get("FAKE_PROMPT_NEVER_COMPLETES")
        os.environ["FAKE_PROMPT_NEVER_COMPLETES"] = "1"
        sleeps: list[float] = []
        try:
            with FakeOpencodeServer() as server:
                bridge = self.make_bridge(server)
                session = bridge.create_session(title="bounded")
                bridge.prompt_async(session["id"], text="bounded", marker="req_b")
                result = bridge_mod.poll_until_terminal(
                    bridge, session["id"], marker="req_b",
                    sleep=sleeps.append, max_attempts=3,
                )
        finally:
            if old is None:
                os.environ.pop("FAKE_PROMPT_NEVER_COMPLETES", None)
            else:
                os.environ["FAKE_PROMPT_NEVER_COMPLETES"] = old
        self.assertEqual(result["status"], "pending")
        self.assertEqual(len(sleeps), 2, "the poll loop must honor its bound")
        # The schedule is the shared bounded-backoff one, not an ad-hoc delay.
        self.assertEqual(sleeps, [budget_mod.backoff_delay(1), budget_mod.backoff_delay(2)])

    def test_poll_loop_stops_at_the_first_terminal_observation(self) -> None:
        sleeps: list[float] = []
        with FakeOpencodeServer() as server:
            bridge = self.make_bridge(server)
            session = bridge.create_session(title="terminal")
            bridge.prompt_async(session["id"], text="done", marker="req_done")
            result = bridge_mod.poll_until_terminal(
                bridge, session["id"], marker="req_done",
                sleep=sleeps.append, max_attempts=3,
            )
        self.assertEqual(result["status"], "completed")
        self.assertEqual(sleeps, [])

    def test_vanished_session_polls_as_terminal_aborted(self) -> None:
        with FakeOpencodeServer() as server:
            bridge = self.make_bridge(server)
            session = bridge.create_session(title="vanish")
            del server.state.sessions[session["id"]]
            result = bridge.result(session["id"], marker="req_x")
        self.assertEqual(result["status"], "aborted")
        self.assertTrue(result["terminal"])

    def test_absent_requested_marker_ignores_older_completed_turns(self) -> None:
        messages = [
            {
                "info": {
                    "id": "msg_old_user",
                    "role": "user",
                    "time": {"created": 1},
                },
                "parts": [
                    {
                        "type": "text",
                        "text": "old request\n\nOPSX-REQUEST-MARKER:req_old",
                    }
                ],
            },
            {
                "info": {
                    "id": "msg_old_assistant",
                    "role": "assistant",
                    "parentID": "msg_old_user",
                    "time": {"created": 2, "completed": 3},
                    "providerID": "fake",
                    "modelID": "fake",
                    "cost": 0.5,
                    "tokens": {
                        "total": 10,
                        "input": 5,
                        "output": 5,
                        "reasoning": 0,
                        "cache": {"read": 0, "write": 0},
                    },
                },
                "parts": [{"type": "text", "text": "old answer"}],
            },
        ]
        # A completed turn from an earlier request is not this request's outcome.
        missing = bridge_mod.parse_result_schema(
            messages, session_id="ses_x", marker="req_missing"
        )
        self.assertFalse(missing["marker_found"])
        self.assertFalse(missing["terminal"])
        self.assertEqual(missing["status"], "pending")
        self.assertIsNone(
            missing["message_id"],
            "an unrelated assistant reply must not be attributed to the absent marker",
        )
        self.assertFalse(missing["usage"]["usage_available"])
        self.assertEqual(missing["cost"]["status"], "unavailable")
        # The older turn still resolves correctly when its own marker is asked for.
        matching = bridge_mod.parse_result_schema(
            messages, session_id="ses_x", marker="req_old"
        )
        self.assertTrue(matching["marker_found"])
        self.assertTrue(matching["terminal"])
        self.assertEqual(matching["status"], "completed")
        self.assertEqual(matching["message_id"], "msg_old_assistant")


# ---------------------------------------------------------------------------
# Identity before prompt and lost-ack recovery
# ---------------------------------------------------------------------------


class IdentityBeforePromptTests(BridgeTestCase):
    def test_identities_are_durable_before_the_side_effect(self) -> None:
        with FakeOpencodeServer() as server:
            journaled = self.journaled(server, process_id=None)
            session = journaled.bridge.create_session(title="identity")
            seen: list[tuple[str, str]] = []
            original = journaled.bridge.prompt_async

            def spy(session_id, **kwargs):
                action = journaled.in_flight_prompt(session_id)
                self.assertIsNotNone(
                    action, "the intent must be journaled before the prompt request"
                )
                detail = json.loads(action["detail"])
                seen.append((detail["request_id"], action["state"]))
                return original(session_id, **kwargs)

            journaled.bridge.prompt_async = spy
            identity = journaled.prompt(session["id"], text="hello")
        request_id, state = seen[0]
        self.assertEqual(request_id, identity.request_id)
        self.assertEqual(state, "dispatched")
        self.assertTrue(identity.acknowledged)
        action = self.ledger.get_action(identity.action_id)
        self.assertEqual(json.loads(action["detail"])["request_id"], identity.request_id)
        dispatch = self.ledger.latest_dispatch(identity.action_id)
        self.assertEqual(dispatch["session_id"], session["id"])
        self.assertIn(identity.request_id, server.state.prompt_calls[0]["body"]["parts"][0]["text"])

    def test_lost_ack_is_recovered_by_lookup_without_a_duplicate_prompt(self) -> None:
        with FakeOpencodeServer() as server:
            server.state.drop_ack_once = True
            journaled = self.journaled(server)
            session = journaled.bridge.create_session(title="lost")
            identity = journaled.prompt(
                session["id"], text="lost-ack",
                reserved_cost_usd=1.0, reserved_elapsed_minutes=0.5,
            )
            self.assertFalse(identity.acknowledged)
            action = self.ledger.get_action(identity.action_id)
            self.assertEqual(action["state"], "uncertain")
            recovery = journaled.recover_lost_ack(identity, sleep=lambda _s: None)
            self.assertFalse(recovery["duplicate_prompt_issued"])
            self.assertEqual(recovery["result"]["status"], "completed")
            self.assertEqual(
                len(server.state.prompt_calls), 1,
                "recovery must never issue a second prompt",
            )
        self.assertEqual(self.ledger.get_action(identity.action_id)["state"], "completed")
        reservation = self.ledger.reservation_for_action(identity.action_id)
        self.assertEqual(reservation["state"], "reconciled")
        self.assertEqual(reservation["observed_cost_usd"], 0.25)

    def test_marker_confirmed_pending_lost_ack_keeps_the_in_flight_guard(self) -> None:
        old = os.environ.get("FAKE_PROMPT_NEVER_COMPLETES")
        os.environ["FAKE_PROMPT_NEVER_COMPLETES"] = "1"
        try:
            with FakeOpencodeServer() as server:
                server.state.drop_ack_once = True
                journaled = self.journaled(server)
                session = journaled.bridge.create_session(title="confirmed-pending")
                identity = journaled.prompt(
                    session["id"], text="still running",
                    reserved_cost_usd=1.0, reserved_elapsed_minutes=0.5,
                )
                self.assertFalse(identity.acknowledged)
                recovery = journaled.recover_lost_ack(
                    identity, sleep=lambda _s: None, max_attempts=2
                )
                # The marker is discoverable, so the prompt was accepted and is
                # genuinely in flight. That is not a resolution: the action must
                # stay in the guard and the reservation must not be released.
                self.assertFalse(recovery["terminal"])
                self.assertFalse(recovery["reconciled"])
                self.assertTrue(recovery["marker_confirmed"])
                self.assertFalse(recovery["duplicate_prompt_issued"])
                self.assertEqual(
                    self.ledger.get_action(identity.action_id)["state"], "uncertain",
                    "a pending poll must not reconcile the action out of the guard",
                )
                with self.assertRaises(bridge_mod.PromptInFlightError):
                    journaled.prompt(session["id"], text="second")
                self.assertEqual(
                    len(server.state.prompt_calls), 1,
                    "no second prompt may be issued while the first is unresolved",
                )
                reservation = self.ledger.reservation_for_action(identity.action_id)
                self.assertEqual(
                    reservation["state"], "reserved",
                    "a marker-confirmed in-flight reservation is retained for its "
                    "eventual observed usage, never released",
                )
                # The unresolved reservation still charges its estimate: budget
                # state is retained, never silently freed.
                self.assertEqual(
                    budget_mod.reservation_charge(dict(reservation))["cost_usd"], 1.0,
                )
                # The turn completes server-side; re-observing now reaches a
                # terminal result and only then releases the guard.
                self.assertTrue(server.state.complete_pending(session["id"]))
                final = journaled.recover_lost_ack(
                    identity, sleep=lambda _s: None, max_attempts=2
                )
                self.assertTrue(final["terminal"])
                self.assertTrue(final["reconciled"])
                self.assertEqual(final["result"]["status"], "completed")
                self.assertEqual(
                    len(server.state.prompt_calls), 1,
                    "terminal reconciliation still issues no second prompt",
                )
                self.assertIsNone(journaled.in_flight_prompt(session["id"]))
        finally:
            if old is None:
                os.environ.pop("FAKE_PROMPT_NEVER_COMPLETES", None)
            else:
                os.environ["FAKE_PROMPT_NEVER_COMPLETES"] = old
        self.assertEqual(self.ledger.get_action(identity.action_id)["state"], "completed")
        reservation = self.ledger.reservation_for_action(identity.action_id)
        self.assertEqual(reservation["state"], "reconciled")
        self.assertEqual(reservation["observed_cost_usd"], 0.25)

    def test_delayed_lost_ack_after_a_completed_prior_turn_stays_guarded(self) -> None:
        with FakeOpencodeServer() as server:
            journaled = self.journaled(server)
            session = journaled.bridge.create_session(title="prior-turn")
            # An earlier, unrelated request already completed in this session.
            server.state.seed_completed_turn(
                session["id"], marker="OPSX-REQUEST-MARKER:req_prior"
            )
            # The new prompt is accepted but its record is withheld (delayed) and
            # the acknowledgement is dropped, so its marker is undiscoverable.
            server.state.drop_ack_once = True
            server.state.hold_prompt_record = True
            identity = journaled.prompt(
                session["id"], text="delayed after prior",
                reserved_cost_usd=1.0, reserved_elapsed_minutes=0.5,
            )
            self.assertFalse(identity.acknowledged)
            recovery = journaled.recover_lost_ack(
                identity, sleep=lambda _s: None, max_attempts=2
            )
            # The completed prior turn belongs to another request and must never
            # be reported as this request's terminal outcome.
            self.assertFalse(recovery["terminal"])
            self.assertFalse(recovery["reconciled"])
            self.assertFalse(recovery["marker_confirmed"])
            self.assertEqual(
                recovery["result"]["status"], "pending",
                "an older completed turn must not resolve the absent marker",
            )
            self.assertFalse(recovery["result"]["marker_found"])
            self.assertFalse(recovery["duplicate_prompt_issued"])
            self.assertEqual(
                self.ledger.get_action(identity.action_id)["state"], "uncertain",
                "an unresolved prompt must stay in the in-flight guard",
            )
            with self.assertRaises(bridge_mod.PromptInFlightError):
                journaled.prompt(session["id"], text="second")
            self.assertEqual(
                len(server.state.prompt_calls), 1,
                "the undiscoverable prompt must never be re-issued",
            )
            reservation = self.ledger.reservation_for_action(identity.action_id)
            self.assertEqual(
                reservation["state"], "retained",
                "an unobservable prompt's unknown consumption is retained",
            )
            self.assertEqual(
                budget_mod.reservation_charge(dict(reservation))["cost_usd"], 1.0,
            )
            # Only the matching delayed record's terminal state resolves it.
            self.assertTrue(server.state.release_held_prompt(session["id"]))
            final = journaled.recover_lost_ack(
                identity, sleep=lambda _s: None, max_attempts=2
            )
            self.assertTrue(final["terminal"])
            self.assertTrue(final["reconciled"])
            self.assertTrue(final["marker_confirmed"])
            self.assertEqual(final["result"]["status"], "completed")
            self.assertEqual(
                len(server.state.prompt_calls), 1,
                "terminal reconciliation through lookup issues no second prompt",
            )
            self.assertIsNone(journaled.in_flight_prompt(session["id"]))
        self.assertEqual(self.ledger.get_action(identity.action_id)["state"], "completed")

    def test_unobservable_lost_ack_retains_the_reservation_and_the_guard(self) -> None:
        with FakeOpencodeServer() as server:
            server.state.drop_ack_once = True
            # The prompt is accepted but no discoverable record is written yet,
            # so lookup cannot find the marker at all.
            server.state.hold_prompt_record = True
            journaled = self.journaled(server)
            session = journaled.bridge.create_session(title="undiscoverable")
            identity = journaled.prompt(
                session["id"], text="delayed",
                reserved_cost_usd=2.0, reserved_elapsed_minutes=0.5,
            )
            self.assertFalse(identity.acknowledged)
            recovery = journaled.recover_lost_ack(
                identity, sleep=lambda _s: None, max_attempts=2
            )
            # No observation is not a resolution: the reservation is retained at
            # its estimate and the action stays in the guard.
            self.assertFalse(recovery["terminal"])
            self.assertFalse(recovery["reconciled"])
            self.assertFalse(recovery["marker_confirmed"])
            self.assertFalse(recovery["duplicate_prompt_issued"])
            self.assertEqual(
                self.ledger.get_action(identity.action_id)["state"], "uncertain",
            )
            with self.assertRaises(bridge_mod.PromptInFlightError):
                journaled.prompt(session["id"], text="second")
            self.assertEqual(len(server.state.prompt_calls), 1)
            reservation = self.ledger.reservation_for_action(identity.action_id)
            self.assertEqual(
                reservation["state"], "retained",
                "an unobservable prompt's unknown consumption is retained, never "
                "released as free",
            )
            self.assertEqual(reservation["reserved_cost_usd"], 2.0)
            self.assertEqual(
                budget_mod.reservation_charge(dict(reservation))["cost_usd"], 2.0,
                "a retained reservation stays charged at its reserved estimate",
            )
            # When the delayed record finally lands, recovery reaches a terminal
            # result without ever re-prompting.
            self.assertTrue(server.state.release_held_prompt(session["id"]))
            final = journaled.recover_lost_ack(
                identity, sleep=lambda _s: None, max_attempts=2
            )
            self.assertTrue(final["terminal"])
            self.assertEqual(final["result"]["status"], "completed")
            self.assertEqual(len(server.state.prompt_calls), 1)
        self.assertEqual(self.ledger.get_action(identity.action_id)["state"], "completed")
        # Retention survives terminal reconciliation: an unknown consumption is
        # resolved on the action, not silently rewritten to observed figures.
        reservation = self.ledger.reservation_for_action(identity.action_id)
        self.assertEqual(reservation["state"], "retained")
        self.assertEqual(reservation["reserved_cost_usd"], 2.0)

    def test_at_most_one_in_flight_prompt_per_session(self) -> None:
        with FakeOpencodeServer() as server:
            journaled = self.journaled(server)
            session = journaled.bridge.create_session(title="serial")
            first = journaled.prompt(session["id"], text="one")
            self.assertTrue(first.acknowledged)
            with self.assertRaises(bridge_mod.PromptInFlightError):
                journaled.prompt(session["id"], text="two")
        self.assertEqual(len(server.state.prompt_calls), 1)

    def test_prompt_dispatch_identity_binds_session_and_process(self) -> None:
        with FakeOpencodeServer() as server:
            identity_process = bridge_mod.serialize_process_identity(os.getpid())
            journaled = self.journaled(server, process_id=identity_process)
            session = journaled.bridge.create_session(title="bind")
            identity = journaled.prompt(session["id"], text="bind me")
        dispatch = self.ledger.latest_dispatch(identity.action_id)
        self.assertEqual(dispatch["session_id"], session["id"])
        self.assertEqual(dispatch["process_id"], identity_process)
        decoded = bridge_mod.parse_process_identity(dispatch["process_id"])
        self.assertEqual(decoded["pid"], os.getpid())
        evidence = self.ledger.list_evidence(identity.action_id)
        self.assertIn("session_binding", [row["kind"] for row in evidence])

    def test_resolve_applies_terminal_result_and_is_idempotent(self) -> None:
        with FakeOpencodeServer() as server:
            journaled = self.journaled(server)
            session = journaled.bridge.create_session(title="resolve")
            identity = journaled.prompt(session["id"], text="resolve")
            result = journaled.bridge.result(session["id"], marker=identity.request_id)
            self.assertEqual(journaled.resolve(identity, result=result), "completed")
            self.assertEqual(journaled.resolve(identity, result=result), "completed")


# ---------------------------------------------------------------------------
# Service-owned session lifetime
# ---------------------------------------------------------------------------


class SessionLifetimeTests(BridgeTestCase):
    @staticmethod
    def _reap(server) -> None:
        """Terminate the stand-in subprocess and close its pipes."""
        try:
            server.terminate()
        except Exception:
            pass
        try:
            server.process.wait(timeout=5)
        except Exception:
            pass
        stream = getattr(server.process, "stdout", None)
        if stream is not None:
            try:
                stream.close()
            except Exception:
                pass

    def test_worker_domain_launch_uses_the_restricted_mechanism_and_records_identity(self) -> None:
        from lib.orchestrator import supervision as supervision_mod

        spawned: list[list[str]] = []

        def recording_factory(argv, **kwargs):
            spawned.append(list(argv))
            # ``/usr/bin/env`` stands in for the restricted-process launcher
            # here: the argv shape is what matters, and a real subprocess gives
            # a real pid/start-time/boot identity to fence.
            return subprocess.Popen(argv, stdout=subprocess.PIPE,
                                    stderr=subprocess.STDOUT, text=True)

        script = self.root / "fake_server.py"
        script.write_text(
            "import sys\n"
            "print('opencode server listening on http://127.0.0.1:4455', flush=True)\n"
            "sys.stdout.flush()\n"
            "import time\n"
            "time.sleep(10)\n",
            encoding="utf-8",
        )
        command = [sys.executable, str(script)]
        server = supervision_mod.launch_session_server(
            switch=["/usr/bin/env"],
            command=command,
            popen_factory=recording_factory,
            boot_timeout=10.0,
        )
        self.addCleanup(server.terminate)
        self.addCleanup(self._reap, server)
        self.assertEqual(server.address, "127.0.0.1:4455")
        # The launcher (canonicalized to its verified absolute path) carries the
        # argv: the model session is started under the worker principal, never
        # with service-identity privileges.
        self.assertTrue(spawned[0][0].startswith("/"), spawned[0][0])
        self.assertEqual(spawned[0][1:], command)
        identity = bridge_mod.parse_process_identity(server.process_identity)
        self.assertEqual(identity["pid"], server.pid)
        self.assertGreater(identity["pid"], 0)
        self.assertNotEqual(identity["pid"], os.getpid())

    def test_launch_refuses_without_a_restricted_mechanism(self) -> None:
        from lib.orchestrator import supervision as supervision_mod

        with self.assertRaises(Exception) as ctx:
            supervision_mod.launch_session_server(switch=[], command=[sys.executable, "-c", ""])
        self.assertIn("no restricted-spawn mechanism", str(ctx.exception))

    def test_job_specific_loopback_address_is_deterministic(self) -> None:
        from lib.orchestrator import supervision as supervision_mod

        self.assertEqual(
            supervision_mod.job_loopback_port(7), supervision_mod.job_loopback_port(7)
        )
        self.assertNotEqual(
            supervision_mod.job_loopback_port(7), supervision_mod.job_loopback_port(8)
        )
        self.assertEqual(supervision_mod.job_loopback_port(None), 0)
        command = supervision_mod.session_server_command(7)
        self.assertEqual(command[:4], ["opencode", "serve", "--hostname", "127.0.0.1"])
        self.assertEqual(command[4], "--port")
        self.assertTrue(command[5].isdigit())
        self.assertGreater(int(command[5]), 0)

    def test_operator_pinned_server_command_is_honored(self) -> None:
        from lib.orchestrator import supervision as supervision_mod

        old = os.environ.get(supervision_mod.ENV_SERVER_COMMAND)
        os.environ[supervision_mod.ENV_SERVER_COMMAND] = "my-server --port 1"
        try:
            self.assertEqual(
                supervision_mod.session_server_command(7), ["my-server", "--port", "1"]
            )
        finally:
            if old is None:
                os.environ.pop(supervision_mod.ENV_SERVER_COMMAND, None)
            else:
                os.environ[supervision_mod.ENV_SERVER_COMMAND] = old

    def test_server_without_a_reported_address_fails_closed(self) -> None:
        from lib.orchestrator import supervision as supervision_mod

        class _Process:
            pid = os.getpid()
            stdout = None

            def terminate(self):
                return None

        server = supervision_mod.SessionServerProcess(
            process=_Process(), command=("x",), worker_command=("x",)
        )
        with self.assertRaises(Exception) as ctx:
            server.await_address(timeout=0.1)
        self.assertIn("no startup stream", str(ctx.exception))

    def test_startup_line_extraction_requires_loopback(self) -> None:
        from lib.orchestrator import supervision as supervision_mod

        self.assertEqual(
            supervision_mod._server_address_from_line(
                "opencode server listening on http://127.0.0.1:4096"
            ),
            "127.0.0.1:4096",
        )
        self.assertIsNone(
            supervision_mod._server_address_from_line(
                "listening on http://0.0.0.0:4096"
            )
        )
        self.assertIsNone(supervision_mod._server_address_from_line("nothing here"))

    def test_adopt_by_lookup_reuses_the_live_session_and_rebriefs(self) -> None:
        from lib.orchestrator import supervision as supervision_mod

        with FakeOpencodeServer() as server:
            bridge = self.make_bridge(server)
            created = bridge.create_session(title="primary")
            linkage = bridge_mod.record_primary_session_linkage(
                self.ledger,
                self.job_id,
                run_id="run-1",
                server_address=server.address,
                session_id=created["id"],
                process_id=bridge_mod.serialize_process_identity(os.getpid()),
            )
            session = _FakeServiceSession(self.ledger, self.job_id)
            runtime = supervision_mod.open_primary_session(
                self.repo, session, transport_factory=bridge_mod.LoopbackTransport.from_address
            )
        self.assertTrue(runtime.adopted)
        self.assertEqual(runtime.session_id, created["id"])
        self.assertEqual(runtime.linkage.session_id, linkage.session_id)
        self.assertEqual(runtime.briefing.mode, "rebrief")
        self.assertEqual(server.state.sessions[created["id"]]["id"], created["id"])
        runtime.close()

    def test_launch_replacement_when_lookup_shows_the_session_is_gone(self) -> None:
        from lib.orchestrator import supervision as supervision_mod

        with FakeOpencodeServer() as server:
            # A linkage naming a session the server no longer has.
            bridge_mod.record_primary_session_linkage(
                self.ledger,
                self.job_id,
                run_id="run-1",
                server_address=server.address,
                session_id="ses_gone",
            )
            session = _FakeServiceSession(self.ledger, self.job_id)
            launcher_calls: list[int] = []

            def launcher():
                launcher_calls.append(1)
                return _FakeServerProcess(server.address, os.getpid())

            runtime = supervision_mod.open_primary_session(
                self.repo,
                session,
                launcher=launcher,
                transport_factory=bridge_mod.LoopbackTransport.from_address,
            )
        self.assertEqual(launcher_calls, [1])
        self.assertFalse(runtime.adopted)
        self.assertNotEqual(runtime.session_id, "ses_gone")
        self.assertEqual(runtime.briefing.mode, "full")
        latest = bridge_mod.primary_session_linkage(self.ledger, self.job_id)
        self.assertEqual(latest.session_id, runtime.session_id)
        runtime.close()

    def test_interactive_attach_uses_the_recorded_linkage(self) -> None:
        linkage = bridge_mod.record_primary_session_linkage(
            self.ledger,
            self.job_id,
            run_id="run-1",
            server_address="127.0.0.1:4444",
            session_id="ses_attach",
            process_id=None,
        )
        target = bridge_mod.attach_target(self.ledger, self.job_id)
        self.assertEqual(target, linkage)
        self.assertEqual(target.server_address, "127.0.0.1:4444")
        self.assertEqual(target.session_id, "ses_attach")

    def test_linkage_is_additive_and_leaves_other_journal_kinds_intact(self) -> None:
        bridge_mod.record_primary_session_linkage(
            self.ledger, self.job_id, run_id="run-1",
            server_address="127.0.0.1:4444", session_id="ses_add",
        )
        rows = self.ledger.list_actions(self.job_id)
        self.assertEqual([r["kind"] for r in rows], ["session_linkage"])
        self.assertEqual(rows[0]["state"], "completed")
        # No schema change: the linkage rides in the existing tables.
        self.assertEqual(self.ledger.schema_version(), ledger.CURRENT_SCHEMA_VERSION)

    def test_service_host_owns_the_primary_session_lifetime(self) -> None:
        from lib.orchestrator import supervision as supervision_mod

        with FakeOpencodeServer() as server:
            host = _FakeHost(self.ledger, self.job_id, self.repo)
            calls: list[str] = []

            def launcher():
                calls.append("launch")
                return _FakeServerProcess(server.address, os.getpid())

            runtime = host.start_primary_session(
                launcher=launcher,
                transport_factory=bridge_mod.LoopbackTransport.from_address,
            )
            # A second call hands back the same managed session: the service
            # owns exactly one primary per job, never a second divergent one.
            again = host.start_primary_session()
            self.assertIs(again, runtime)
            self.assertEqual(calls, ["launch"])
            self.assertEqual(
                bridge_mod.primary_session_linkage(self.ledger, self.job_id).session_id,
                runtime.session_id,
            )
            host.close()
        self.assertTrue(runtime.server.terminated)
        self.assertTrue(server.state.sessions)
        self.assertIsNone(host.primary_session)

    def test_supervise_serve_cli_reports_a_named_failure_when_startup_fails(self) -> None:
        import argparse
        import io
        from contextlib import redirect_stderr, redirect_stdout

        from lib.orchestrator import cmd_supervise
        from lib.orchestrator import supervision as supervision_mod

        plan = self.repo / "plan.toml"
        plan.write_text(
            "[plan]\nname = \"bridge-test\"\nmax_rounds = 1\n\n"
            "[[changes]]\nid = \"add-opencode-session-bridge\"\n"
            "phase = 1\npause_before = false\ndepends_on = []\n",
            encoding="utf-8",
        )
        env = mock.patch.dict(
            os.environ,
            {
                supervision_mod.ENV_STATE_FILE: str(self.db_path),
                supervision_mod.ENV_SERVER_COMMAND: "/nonexistent/server",
            },
        )
        with env, mock.patch.object(
            supervision_mod, "launch_session_server",
            side_effect=supervision_mod.broker_mod.BrokerUnavailableError(
                "no restricted-spawn mechanism is available"
            ),
        ), mock.patch.object(
            supervision_mod, "_operator_allowed_uids",
            return_value=frozenset({os.getuid()}),
        ), mock.patch.object(
            supervision_mod, "_allowed_uids_for",
            return_value=frozenset({os.getuid()}),
        ):
            args = argparse.Namespace(
                repo=str(self.repo),
                plan=str(plan),
                store=str(self.db_path),
                job_id=None,
                once=True,
                timeout=0.1,
                primary_session=True,
            )
            with redirect_stdout(io.StringIO()) as out, redirect_stderr(io.StringIO()) as err:
                code = cmd_supervise.cmd_supervise_serve(args)
        self.assertEqual(code, 1)
        self.assertIn("BrokerUnavailableError", err.getvalue())
        self.assertIn("no restricted-spawn mechanism", err.getvalue())
        self.assertIn("is live", out.getvalue())


class PrimarySessionIntegrationTests(BridgeTestCase):
    """End-to-end loopback coverage: the managed primary is actually driven.

    These tests exercise ``open_primary_session`` (the service entry point) and
    assert the two behaviours review found missing: the bounded full/rebrief
    briefing text is dispatched to the created or adopted session, and a
    primary prompt is journaled, reserved, dispatched, and reconciled through
    the journaled bridge rather than performed outside it.
    """

    @staticmethod
    def _prompt_texts(server) -> list[str]:
        return [
            call["body"]["parts"][0]["text"] for call in server.state.prompt_calls
        ]

    def _action_kinds(self) -> list[str]:
        return [row["kind"] for row in self.ledger.list_actions(self.job_id)]

    def test_replacement_session_receives_the_full_briefing_through_the_bridge(self) -> None:
        from lib.orchestrator import supervision as supervision_mod

        # Durable blocking truth the briefing must surface: an unreconciled
        # uncertain action is rendered as blocking, never summarized away.
        uncertain_id = self.ledger.begin_action(
            self.job_id,
            kind="implement",
            run_id="run-1",
            detail=json.dumps({"request_id": "req_preexisting"}),
        )
        self.ledger.dispatch_action(uncertain_id)
        self.ledger.mark_uncertain(
            uncertain_id, detail=json.dumps({"uncertainty": "prompt_unresolved"})
        )
        with FakeOpencodeServer() as server:
            session = _FakeServiceSession(self.ledger, self.job_id)
            runtime = supervision_mod.open_primary_session(
                self.repo,
                session,
                launcher=lambda: _FakeServerProcess(server.address, os.getpid()),
                transport_factory=bridge_mod.LoopbackTransport.from_address,
            )
            prompts = self._prompt_texts(server)
            self.assertFalse(runtime.adopted)
            self.assertEqual(runtime.briefing.mode, "full")
            self.assertEqual(
                len(prompts), 1,
                "the replacement session must be briefed through the bridge",
            )
            # The dispatched text is the composed full briefing, not a stub: it
            # carries the durable job/authority context, with the journaled
            # request marker appended so a lost ack stays recoverable.
            self.assertTrue(
                prompts[0].startswith(runtime.briefing.render()),
                "the session must receive the composed full briefing text",
            )
            self.assertIsNotNone(bridge_mod.extract_marker(prompts[0]))
            self.assertIn("[job]", prompts[0])
            self.assertIn("[blocking] (blocking)", prompts[0])
            self.assertIn(f"UNCERTAIN action {uncertain_id}", prompts[0])
            runtime.close()

    def test_adopted_session_receives_the_rebrief_through_the_bridge(self) -> None:
        from lib.orchestrator import supervision as supervision_mod

        with FakeOpencodeServer() as server:
            bridge = self.make_bridge(server)
            created = bridge.create_session(title="primary")
            bridge_mod.record_primary_session_linkage(
                self.ledger,
                self.job_id,
                run_id="run-1",
                server_address=server.address,
                session_id=created["id"],
                process_id=bridge_mod.serialize_process_identity(os.getpid()),
            )
            session = _FakeServiceSession(self.ledger, self.job_id)
            runtime = supervision_mod.open_primary_session(
                self.repo,
                session,
                transport_factory=bridge_mod.LoopbackTransport.from_address,
            )
            prompts = self._prompt_texts(server)
            self.assertTrue(runtime.adopted)
            self.assertEqual(runtime.briefing.mode, "rebrief")
            self.assertEqual(
                len(prompts), 1,
                "the adopted session must receive a bounded re-brief",
            )
            self.assertTrue(
                prompts[0].startswith(runtime.briefing.render()),
                "the adopted session must receive the bounded re-brief text",
            )
            self.assertIsNotNone(bridge_mod.extract_marker(prompts[0]))
            self.assertTrue(prompts[0].startswith("# session briefing (rebrief"))
            # Delivery landed on the adopted session, not a replacement.
            self.assertEqual(server.state.prompt_calls[0]["session_id"], created["id"])
            runtime.close()

    def test_briefing_is_journaled_reserved_dispatched_and_reconciled(self) -> None:
        from lib.orchestrator import supervision as supervision_mod

        with FakeOpencodeServer() as server:
            session = _FakeServiceSession(self.ledger, self.job_id)
            runtime = supervision_mod.open_primary_session(
                self.repo,
                session,
                launcher=lambda: _FakeServerProcess(server.address, os.getpid()),
                transport_factory=bridge_mod.LoopbackTransport.from_address,
            )
            runtime.close()

        rows = self.ledger.list_actions(self.job_id)
        kinds = [row["kind"] for row in rows]
        self.assertIn("session_linkage", kinds)
        prompt_rows = [r for r in rows if r["kind"] == bridge_mod.PROMPT_ACTION_KIND]
        self.assertEqual(
            len(prompt_rows), 1,
            "the delivered briefing must be exactly one journaled prompt",
        )
        briefing_action = prompt_rows[0]
        detail = json.loads(briefing_action["detail"])
        self.assertEqual(detail["stage"], "briefing")
        self.assertEqual(detail["session_id"], runtime.session_id)
        self.assertTrue(detail["request_id"].startswith("req_"))
        self.assertEqual(briefing_action["state"], "completed")

        dispatch = self.ledger.latest_dispatch(int(briefing_action["id"]))
        self.assertIsNotNone(dispatch, "the briefing prompt must be dispatched")
        self.assertEqual(dispatch["session_id"], runtime.session_id)
        self.assertIsNotNone(dispatch["process_id"], "server identity is bound")

        reservation = self.ledger.reservation_for_action(int(briefing_action["id"]))
        self.assertIsNotNone(reservation, "the prompt must be budget-reserved")
        self.assertEqual(reservation["role"], model_policy.SUPERVISOR_ROLE)
        self.assertEqual(
            reservation["state"], "reconciled",
            "observed usage must be reconciled after the terminal result",
        )
        self.assertGreater(reservation["observed_cost_usd"], 0.0)
        evidence = {row["kind"] for row in self.ledger.list_evidence(
            int(briefing_action["id"])
        )}
        self.assertIn(bridge_mod.EVIDENCE_SESSION_BINDING, evidence)
        self.assertIn(bridge_mod.EVIDENCE_STAGE_RESULT, evidence)
        self.assertIn(bridge_mod.EVIDENCE_USAGE, evidence)

    def test_primary_prompt_after_briefing_creates_and_reconciles_its_own_action(self) -> None:
        from lib.orchestrator import supervision as supervision_mod

        with FakeOpencodeServer() as server:
            session = _FakeServiceSession(self.ledger, self.job_id)
            runtime = supervision_mod.open_primary_session(
                self.repo,
                session,
                launcher=lambda: _FakeServerProcess(server.address, os.getpid()),
                transport_factory=bridge_mod.LoopbackTransport.from_address,
            )
            outcome = runtime.dispatch_prompt("drive the round", stage="primary")
            runtime.close()

        self.assertEqual(outcome["outcome"], "completed")
        self.assertTrue(outcome["reconciled"])
        self.assertFalse(outcome["duplicate_prompt_issued"])
        prompt_rows = [
            r for r in self.ledger.list_actions(self.job_id)
            if r["kind"] == bridge_mod.PROMPT_ACTION_KIND
        ]
        self.assertEqual(len(prompt_rows), 2, "briefing plus primary prompt")
        primary = prompt_rows[-1]
        self.assertEqual(json.loads(primary["detail"])["stage"], "primary")
        # The primary prompt carries its own request marker and reservation.
        self.assertIn("drive the round", self._prompt_texts(server)[-1])
        reservation = self.ledger.reservation_for_action(int(primary["id"]))
        self.assertEqual(reservation["state"], "reconciled")
        # Replay of the same terminal result never double-bills.
        reservations = self.ledger.reservations_for_job(self.job_id)
        self.assertEqual(len(reservations), 2)
        consumption = budget_mod.sum_reservations([dict(r) for r in reservations])
        self.assertEqual(consumption["reservation_count"], 2)

    def test_replacement_is_torn_down_when_the_briefing_cannot_be_delivered(self) -> None:
        from lib.orchestrator import supervision as supervision_mod

        with FakeOpencodeServer() as server:
            session = _FakeServiceSession(self.ledger, self.job_id)
            created: list[_FakeServerProcess] = []

            def launcher():
                process = _FakeServerProcess(server.address, os.getpid())
                created.append(process)
                return process

            with mock.patch.object(
                supervision_mod,
                "_apply_briefing",
                side_effect=supervision_mod.broker_mod.BrokerUnavailableError(
                    "briefing dispatch failed"
                ),
            ):
                with self.assertRaises(supervision_mod.broker_mod.BrokerUnavailableError):
                    supervision_mod.open_primary_session(
                        self.repo,
                        session,
                        launcher=launcher,
                        transport_factory=bridge_mod.LoopbackTransport.from_address,
                    )
        # A primary that could not be briefed is not returned, and its headless
        # server is not leaked.
        self.assertEqual(len(created), 1)
        self.assertTrue(created[0].terminated)

    def test_service_host_creates_one_primary_and_briefs_it(self) -> None:
        from lib.orchestrator import supervision as supervision_mod

        with FakeOpencodeServer() as server:
            host = _FakeHost(self.ledger, self.job_id, self.repo)
            calls: list[str] = []

            def launcher():
                calls.append("launch")
                return _FakeServerProcess(server.address, os.getpid())

            runtime = host.start_primary_session(
                launcher=launcher,
                transport_factory=bridge_mod.LoopbackTransport.from_address,
            )
            again = host.start_primary_session()
            host.close()
        # One primary per job, adopt-before-replace, and the delivered briefing
        # rides the same linkage the operator attaches through.
        self.assertIs(again, runtime)
        self.assertEqual(calls, ["launch"])
        self.assertEqual(len(server.state.prompt_calls), 1)
        linkage = bridge_mod.primary_session_linkage(self.ledger, self.job_id)
        self.assertEqual(linkage.session_id, runtime.session_id)


class _FakeHost:
    """Minimal stand-in for ``ServiceEndpointHost``'s session-owning surface."""

    def __init__(self, handle, job_id, repo):
        from lib.orchestrator import supervision as supervision_mod

        self.session = _FakeServiceSession(handle, job_id)
        self.session.repo = repo
        self.store_path = Path("/tmp/store.sqlite3")
        self.primary_session = None
        self._impl = supervision_mod.ServiceEndpointHost(
            session=self.session,
            store_path=self.store_path,
            operator_allowed_uids=frozenset(),
            service_allowed_uids=frozenset(),
            operator_socket=Path("/tmp/op.sock"),
            worker_socket=Path("/tmp/wk.sock"),
        )

    def start_primary_session(self, **kwargs):
        return self._impl.start_primary_session(**kwargs)

    def close(self) -> None:
        self._impl.close()
        self.primary_session = self._impl.primary_session


class _FakeServiceSession:
    def __init__(self, handle, job_id):
        self.ledger = handle
        self.job_id = job_id


class _FakeServerProcess:
    def __init__(self, address, pid):
        self.address = address
        self.pid = pid
        self.terminated = False

    @property
    def process_identity(self):
        return bridge_mod.serialize_process_identity(self.pid)

    def terminate(self):
        self.terminated = True


# ---------------------------------------------------------------------------
# Reconnect briefing from authority plus ledger
# ---------------------------------------------------------------------------


class BriefingTests(BridgeTestCase):
    def test_briefing_is_composed_from_durable_state_not_transcript(self) -> None:
        self.ledger.record_incident(self.job_id, kind="review_rejection",
                                    summary="reviewer rejected round 1")
        self.ledger.record_incident_attempt(self.job_id, signature="review_rejection")
        self.ledger.record_incident_attempt(self.job_id, signature="review_rejection")
        action_id = self.ledger.begin_action(
            self.job_id, kind=bridge_mod.PROMPT_ACTION_KIND, run_id="run-1",
            detail=json.dumps({"request_id": "req_uncertain", "session_id": "ses_1"}),
        )
        self.ledger.dispatch_action(action_id, session_id="ses_1")
        self.ledger.mark_uncertain(action_id, detail=json.dumps(
            {"request_id": "req_uncertain", "uncertainty": "prompt_unresolved"}
        ))
        briefing = bridge_mod.compose_briefing(
            self.ledger, self.job_id, mode="full", change_id="add-opencode-session-bridge"
        )
        names = [section.name for section in briefing.sections]
        self.assertEqual(
            names,
            ["job", "plan_snapshot", "blocking", "budget", "incidents", "failed_remedies"],
        )
        rendered = briefing.render()
        self.assertIn(str(self.job_id), rendered)
        self.assertIn("req_uncertain", rendered)
        self.assertIn("review_rejection", rendered)
        self.assertIn("attempted 2 time(s)", rendered)
        self.assertNotIn("transcript", rendered.lower())

    def test_uncertain_action_is_rendered_as_blocking_state(self) -> None:
        action_id = self.ledger.begin_action(
            self.job_id, kind=bridge_mod.PROMPT_ACTION_KIND, run_id="run-1",
            detail=json.dumps({"request_id": "req_block", "session_id": "ses_1"}),
        )
        self.ledger.dispatch_action(action_id, session_id="ses_1")
        self.ledger.mark_uncertain(action_id)
        briefing = bridge_mod.compose_briefing(self.ledger, self.job_id, mode="full")
        blocking = briefing.blocking_sections
        self.assertEqual([s.name for s in blocking], ["blocking"])
        self.assertIn("UNCERTAIN", blocking[0].render())

    def test_briefing_stays_within_its_bound(self) -> None:
        sections = [
            bridge_mod.BriefingSection(
                name="detail", blocking=False,
                lines=tuple(f"non-blocking detail line {i}" for i in range(200)),
            ),
            bridge_mod.BriefingSection(
                name="blocking", blocking=True, lines=("must keep",)
            ),
        ]
        bounded, truncated = bridge_mod.bound_sections(sections, 120)
        self.assertTrue(truncated)
        kept = {section.name: section for section in bounded}
        self.assertEqual(kept["blocking"].lines, ("must keep",))
        self.assertLess(len(kept["detail"].lines), 200)

    def test_blocking_content_is_never_shed_even_over_bound(self) -> None:
        sections = [
            bridge_mod.BriefingSection(
                name="blocking", blocking=True,
                lines=tuple(f"blocking {i}" for i in range(50)),
            )
        ]
        bounded, _truncated = bridge_mod.bound_sections(sections, 10)
        self.assertEqual(bounded[0].lines, sections[0].lines)

    def test_rebrief_bound_is_smaller_than_the_full_bound(self) -> None:
        self.assertEqual(bridge_mod.BRIEFING_BOUND_CHARS, 6000)
        self.assertLess(bridge_mod.REBRIEF_BOUND_CHARS, bridge_mod.BRIEFING_BOUND_CHARS)
        full = bridge_mod.compose_briefing(self.ledger, self.job_id, mode="full")
        rebrief = bridge_mod.compose_briefing(self.ledger, self.job_id, mode="rebrief")
        self.assertEqual(full.bound, bridge_mod.BRIEFING_BOUND_CHARS)
        self.assertEqual(rebrief.bound, bridge_mod.REBRIEF_BOUND_CHARS)

    def test_unknown_briefing_mode_is_refused(self) -> None:
        with self.assertRaises(bridge_mod.BridgeJournalError):
            bridge_mod.compose_briefing(self.ledger, self.job_id, mode="transcript")


# ---------------------------------------------------------------------------
# Hint-only event handling
# ---------------------------------------------------------------------------


class EventHintTests(BridgeTestCase):
    def test_parser_yields_typed_hints_and_tolerates_malformed_frames(self) -> None:
        payload = (
            "data: {\"id\":\"evt_1\",\"type\":\"message.updated\","
            "\"properties\":{\"sessionID\":\"ses_1\",\"messageID\":\"msg_1\"}}\n\n"
            "data: not json at all\n\n"
            ": comment only\n\n"
            "data: {\"id\":\"evt_2\",\"type\":\"message.part.updated\","
            "\"properties\":{\"sessionID\":\"ses_1\",\"messageID\":\"msg_1\","
            "\"partID\":\"prt_1\"}}\n\n"
            "data: {\"id\":\"evt_3\",\"type\":\"session.idle\",\"properties\""
        ).encode("utf-8")
        hints = list(bridge_mod.parse_event_stream([payload[:40], payload[40:]]))
        self.assertEqual([hint.kind for hint in hints], ["message.updated", "message.part.updated"])
        self.assertEqual(hints[0].session_id, "ses_1")
        self.assertEqual(hints[1].part_id, "prt_1")

    def test_duplicate_and_out_of_order_events_are_applied_once(self) -> None:
        hint = bridge_mod.SessionEventHint(
            event_id="evt_dup", kind="message.part.updated",
            session_id="ses_1", message_id="msg_1", part_id="prt_1",
        )
        consumer = bridge_mod.HintConsumer(session_id="ses_1")
        self.assertTrue(consumer.observe(hint))
        self.assertFalse(consumer.observe(hint))
        self.assertFalse(consumer.observe(hint))
        self.assertEqual(consumer.duplicates, 2)
        self.assertEqual(consumer.recorded_outcomes, 0)
        # Out-of-order: two distinct events arriving in reverse id order are
        # both new hints, and nothing is recorded as an outcome.
        later = bridge_mod.SessionEventHint(
            event_id="evt_later", kind="session.idle", session_id="ses_1",
            message_id="msg_2",
        )
        earlier = bridge_mod.SessionEventHint(
            event_id="evt_earlier", kind="message.updated", session_id="ses_1",
            message_id="msg_0",
        )
        self.assertTrue(consumer.observe(later))
        self.assertTrue(consumer.observe(earlier))
        # The original duplicate is still recognized, in any arrival order.
        self.assertFalse(consumer.observe(hint))
        self.assertEqual(consumer.recorded_outcomes, 0)

    def test_hints_never_record_outcomes_and_orphans_are_evidence_only(self) -> None:
        with FakeOpencodeServer() as server:
            journaled = self.journaled(server)
            session = journaled.bridge.create_session(title="hints")
            identity = journaled.prompt(session["id"], text="hint me")
            hint = bridge_mod.SessionEventHint(
                event_id="evt_m", kind="message.updated",
                session_id=session["id"], message_id="msg_1",
            )
            matched = journaled.observe_hint(hint)
            duplicate = journaled.observe_hint(
                bridge_mod.SessionEventHint(
                    event_id="evt_m2", kind="message.updated",
                    session_id=session["id"], message_id="msg_1",
                )
            )
            orphan = journaled.observe_hint(
                bridge_mod.SessionEventHint(
                    event_id="evt_o", kind="message.updated",
                    session_id="ses_orphan", message_id="msg_o",
                )
            )
        self.assertTrue(matched["recorded"])
        self.assertEqual(matched["matched_action_id"], identity.action_id)
        self.assertTrue(duplicate["duplicate"])
        self.assertFalse(duplicate["recorded"])
        self.assertTrue(orphan["orphan"])
        self.assertFalse(orphan["recorded"])
        # The action is still non-terminal: a hint is never an outcome.
        self.assertEqual(self.ledger.get_action(identity.action_id)["state"], "dispatched")
        rows = self.ledger.list_evidence(identity.action_id)
        kinds = [row["kind"] for row in rows]
        self.assertIn(bridge_mod.EVIDENCE_HINT, kinds)
        self.assertEqual(kinds.count(bridge_mod.EVIDENCE_HINT), 1)

    def test_stream_loss_converges_by_polling_without_replay(self) -> None:
        with FakeOpencodeServer() as server:
            journaled = self.journaled(server)
            session = journaled.bridge.create_session(title="loss")
            identity = journaled.prompt(session["id"], text="loss")
            old = os.environ.get("FAKE_EVENT_SCRIPT")
            os.environ["FAKE_EVENT_SCRIPT"] = json.dumps(
                [
                    {
                        "id": "evt_seen",
                        "type": "message.updated",
                        "properties": {"sessionID": session["id"], "messageID": "msg_1"},
                    }
                ]
            )
            try:
                consumer = bridge_mod.HintConsumer(session_id=session["id"])
                accepted = consumer.consume(journaled.bridge.open_event_stream())
                consumer.stream_lost = True
                result = consumer.converge(
                    journaled.bridge, session["id"], marker=identity.request_id,
                    sleep=lambda _s: None,
                )
            finally:
                if old is None:
                    os.environ.pop("FAKE_EVENT_SCRIPT", None)
                else:
                    os.environ["FAKE_EVENT_SCRIPT"] = old
        self.assertEqual([hint.event_id for hint in accepted], ["evt_seen"])
        self.assertEqual(result["status"], "completed")
        self.assertEqual(result["source"], "poll")
        call_paths = [path for _m, path in server.state.requests]
        self.assertIn(f"/session/{session['id']}/message", call_paths)
        self.assertNotIn(f"/session/{session['id']}/message?replay=1", call_paths)

    def test_event_stream_disconnect_is_a_transport_failure_not_a_lost_outcome(self) -> None:
        with FakeOpencodeServer() as server:
            bridge = self.make_bridge(server)
            old = os.environ.get("FAKE_EVENT_DISCONNECT")
            os.environ["FAKE_EVENT_DISCONNECT"] = "1"
            try:
                consumer = bridge_mod.HintConsumer()
                accepted = consumer.consume(bridge.open_event_stream())
            finally:
                if old is None:
                    os.environ.pop("FAKE_EVENT_DISCONNECT", None)
                else:
                    os.environ["FAKE_EVENT_DISCONNECT"] = old
        self.assertEqual(accepted, [])
        self.assertTrue(consumer.stream_lost)
        self.assertEqual(consumer.recorded_outcomes, 0)


# ---------------------------------------------------------------------------
# Budget routing
# ---------------------------------------------------------------------------


class BudgetRoutingTests(BridgeTestCase):
    def test_reservation_exists_before_the_prompt_side_effect(self) -> None:
        with FakeOpencodeServer() as server:
            journaled = self.journaled(server)
            session = journaled.bridge.create_session(title="budget")
            observed: list[str] = []
            original = journaled.bridge.prompt_async

            def spy(session_id, **kwargs):
                reservation = journaled.ledger.reservation_for_action(
                    journaled.in_flight_prompt(session_id)["id"]
                )
                observed.append(reservation["state"] if reservation else "missing")
                return original(session_id, **kwargs)

            journaled.bridge.prompt_async = spy
            identity = journaled.prompt(
                session["id"], text="budget",
                reserved_cost_usd=1.0, reserved_elapsed_minutes=0.5,
            )
        self.assertEqual(observed, ["reserved"])
        reservation = self.ledger.reservation_for_action(identity.action_id)
        self.assertEqual(reservation["role"], model_policy.SUPERVISOR_ROLE)
        self.assertEqual(reservation["reserved_cost_usd"], 1.0)

    def test_reconcile_after_terminal_state_records_observed_usage(self) -> None:
        with FakeOpencodeServer() as server:
            journaled = self.journaled(server)
            session = journaled.bridge.create_session(title="reconcile")
            identity = journaled.prompt(
                session["id"], text="reconcile",
                reserved_cost_usd=1.0, reserved_elapsed_minutes=0.5,
            )
            result = journaled.bridge.result(session["id"], marker=identity.request_id)
            self.assertEqual(journaled.resolve(identity, result=result), "completed")
        reservation = self.ledger.reservation_for_action(identity.action_id)
        self.assertEqual(reservation["state"], "reconciled")
        self.assertEqual(reservation["observed_output_tokens"], 10)
        self.assertEqual(reservation["observed_cost_usd"], 0.25)
        # Observed usage is journaled through the shared evidence vocabulary.
        kinds = [row["kind"] for row in self.ledger.list_evidence(identity.action_id)]
        self.assertIn(bridge_mod.EVIDENCE_USAGE, kinds)

    def test_replay_does_not_double_bill(self) -> None:
        with FakeOpencodeServer() as server:
            journaled = self.journaled(server)
            session = journaled.bridge.create_session(title="double")
            identity = journaled.prompt(
                session["id"], text="double",
                reserved_cost_usd=1.0, reserved_elapsed_minutes=0.5,
            )
            result = journaled.bridge.result(session["id"], marker=identity.request_id)
            journaled.resolve(identity, result=result)
            journaled.resolve(identity, result=result)
            journaled.reconcile_prompt_reservation(identity.action_id, result)
        reservations = self.ledger.reservations_for_job(self.job_id)
        self.assertEqual(len(reservations), 1)
        consumption = budget_mod.sum_reservations(
            [dict(row) for row in reservations]
        )
        self.assertEqual(consumption["cost_usd"], 0.25)
        self.assertEqual(consumption["reconciled_cost_usd"], 0.25)

    def test_interrupted_prompt_retains_its_reservation(self) -> None:
        with FakeOpencodeServer() as server:
            journaled = self.journaled(server)
            session = journaled.bridge.create_session(title="retain")
            identity = journaled.prompt(
                session["id"], text="retain",
                reserved_cost_usd=2.0, reserved_elapsed_minutes=0.5,
            )
            retained = journaled.retain_unknown_reservation(identity.action_id)
            # Retaining is idempotent: a duplicate interrupted observation never
            # releases or rewrites the reservation.
            journaled.retain_unknown_reservation(identity.action_id)
        self.assertTrue(retained)
        reservation = self.ledger.reservation_for_action(identity.action_id)
        self.assertEqual(reservation["state"], "retained")
        self.assertEqual(reservation["reserved_cost_usd"], 2.0)

    def test_unknown_usage_retains_rather_than_releases(self) -> None:
        with FakeOpencodeServer() as server:
            bridge = self.make_bridge(server)
            journaled = self.journaled(server)
            session = bridge.create_session(title="unknown")
            identity = journaled.prompt(
                session["id"], text="unknown",
                reserved_cost_usd=1.5, reserved_elapsed_minutes=0.5,
            )
            # A pending result carries no observed usage: the reservation is
            # retained, never released.
            pending = bridge_mod.parse_result_schema(
                [], session_id=session["id"], marker=identity.request_id
            )
            self.assertFalse(
                pending["marker_found"],
                "a requested marker must not be echoed as if it were observed",
            )
            journaled.resolve(identity, result=pending)
        reservation = self.ledger.reservation_for_action(identity.action_id)
        self.assertEqual(reservation["state"], "retained")
        self.assertEqual(reservation["reserved_cost_usd"], 1.5)


# ---------------------------------------------------------------------------
# Journal discipline
# ---------------------------------------------------------------------------


class JournalDisciplineTests(BridgeTestCase):
    def test_bridge_creates_no_records_for_an_unregistered_run(self) -> None:
        with FakeOpencodeServer() as server:
            transport = bridge_mod.LoopbackTransport.from_address(server.address)
            self.addCleanup(transport.close)
            bridge = bridge_mod.SessionBridge(transport)
            bridge.check_capability()
            session = bridge.create_session(title="legacy")
            bridge.prompt_async(session["id"], text="legacy")
        self.assertEqual(self.ledger.list_actions(self.job_id), [])
        self.assertEqual(self.ledger.reservations_for_job(self.job_id), [])

    def test_prompt_action_kind_and_evidence_vocabulary(self) -> None:
        with FakeOpencodeServer() as server:
            journaled = self.journaled(server)
            session = journaled.bridge.create_session(title="vocab")
            identity = journaled.prompt(session["id"], text="vocab")
            result = journaled.bridge.result(session["id"], marker=identity.request_id)
            journaled.resolve(identity, result=result)
        action = self.ledger.get_action(identity.action_id)
        self.assertEqual(action["kind"], bridge_mod.PROMPT_ACTION_KIND)
        kinds = {row["kind"] for row in self.ledger.list_evidence(identity.action_id)}
        self.assertTrue(
            kinds.issubset(
                {
                    bridge_mod.EVIDENCE_SESSION_BINDING,
                    bridge_mod.EVIDENCE_STAGE_RESULT,
                    bridge_mod.EVIDENCE_USAGE,
                    bridge_mod.EVIDENCE_SPAWN_LOSS,
                    bridge_mod.EVIDENCE_HINT,
                    # The named pre-prompt transport gate records its decision
                    # as additive action evidence before the dispatch record.
                    agent_contracts_mod.EVIDENCE_TRANSPORT_DECISION,
                }
            )
        )

    def test_failed_prompt_is_terminal_prefixed_by_reconciliation(self) -> None:
        with FakeOpencodeServer() as server:
            journaled = self.journaled(server)
            session = journaled.bridge.create_session(title="fail")
            identity = journaled.prompt(session["id"], text="fail")
            # Simulate a lost ack so the action becomes uncertain, then resolve
            # it to a confirmed failure through evidence + reconciliation.
            self.ledger.mark_uncertain(identity.action_id)
            result = {
                "status": "error",
                "usage": {"usage_available": True, "input_tokens": 1,
                          "output_tokens": 1, "cached_input_tokens": 0,
                          "reasoning_tokens": 0},
                "cost": {"status": "estimated", "estimated_cost": 0.1},
                "duration_ms": 5,
                "marker": identity.request_id,
            }
            self.assertEqual(journaled.resolve(identity, result=result), "failed")
        self.assertEqual(self.ledger.get_action(identity.action_id)["state"], "failed")


class ClockDisciplineTests(BridgeTestCase):
    def test_capability_timestamp_uses_the_package_clock(self) -> None:
        # Patch the clock module the bridge itself resolves through, so the
        # assertion holds regardless of any module-identity churn elsewhere in
        # the suite.
        original = bridge_mod.clock_mod.utcnow
        bridge_mod.clock_mod.utcnow = lambda: "2001-01-01T00:00:00+00:00"
        try:
            with FakeOpencodeServer() as server:
                bridge = self.make_bridge(server)
                capability = bridge.capability
        finally:
            bridge_mod.clock_mod.utcnow = original
        self.assertEqual(capability.checked_at, "2001-01-01T00:00:00+00:00")

    def test_journal_timestamps_use_the_package_clock(self) -> None:
        marker = "2001-01-01T00:00:00+00:00"
        original = bridge_mod.clock_mod.utcnow
        bridge_mod.clock_mod.utcnow = lambda: marker
        try:
            linkage = bridge_mod.record_primary_session_linkage(
                self.ledger, self.job_id, run_id="run-1",
                server_address="127.0.0.1:4444", session_id="ses_clock",
            )
        finally:
            bridge_mod.clock_mod.utcnow = original
        self.assertEqual(linkage.recorded_at, marker)
        rows = self.ledger.list_actions(self.job_id)
        self.assertEqual(rows[-1]["intent_at"], marker)

    def test_process_identity_helpers_round_trip(self) -> None:
        serialized = bridge_mod.serialize_process_identity(os.getpid())
        decoded = bridge_mod.parse_process_identity(serialized)
        self.assertEqual(decoded["pid"], os.getpid())
        self.assertIsNone(bridge_mod.parse_process_identity("not json"))
        self.assertIsNone(bridge_mod.parse_process_identity({"pid": 0}))


class LockContractTests(BridgeTestCase):
    def test_process_identity_matches_the_lock_module_shape(self) -> None:
        identity = lock_mod.current_identity()
        decoded = bridge_mod.parse_process_identity(
            bridge_mod.serialize_process_identity(identity["pid"])
        )
        self.assertEqual(decoded["boot_id"], identity["boot_id"])
        self.assertEqual(decoded["process_start"], identity["process_start"])


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
