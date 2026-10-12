"""Focused regression tests for the supervision service host endpoints.

Covers the narrow host-provisioning fix required to run the email-plan
supervisor service under distinct operator/service/worker principals:

- the explicitly provisioned operator principal
  (``OPSX_SUPERVISOR_OPERATOR_PRINCIPAL``) and the operator allowlist that
  excludes the service and worker principals;
- the worker-actions allowlist (service plus provisioned worker, never the
  operator);
- the provisioned named socket groups (``OPSX_SUPERVISOR_OPERATOR_GROUP`` /
  ``OPSX_SUPERVISOR_WORKER_GROUP``) with service-owned ``0660`` sockets, the
  worker-admitting operator group refusal, and the secure-parent rule for
  sockets outside the ledger home;
- per-connection fault containment: malformed JSON, partial requests,
  disconnects, and failed response writes must not tear down the serve loop,
  while authority and ledger faults are still surfaced;
- the canonical supervision skill's frontmatter is parseable YAML.

Everything here is standard library only and runs without host principal
changes; principal uid/gid resolution is injected or mocked where the real
host would need provisioned accounts.
"""

from __future__ import annotations

import json
import os
import pwd
import socket
import stat
import struct
import tempfile
import threading
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

from lib.orchestrator import supervision as supervision_mod
from lib.supervisor import authority as authority_mod
from lib.supervisor import broker as broker_mod
from lib.supervisor import broker_client
from lib.supervisor import endpoints as endpoints_mod
from lib.supervisor import ledger as ledger_mod

try:  # PyYAML is not a declared test dependency; the parse check is optional.
    import yaml
except ImportError:  # pragma: no cover - depends on the runner
    yaml = None

REPO_ROOT = Path(__file__).resolve().parents[2]
SKILL_PATH = REPO_ROOT / "skills" / "opsx-supervision" / "SKILL.md"


def _current_user_name() -> str:
    return pwd.getpwuid(os.getuid()).pw_name


# ---------------------------------------------------------------------------
# Test doubles
# ---------------------------------------------------------------------------


class _FakeConn:
    """A minimal server-side connection with controllable transport behavior."""

    def __init__(
        self,
        request: bytes,
        *,
        fail_write: bool = False,
        recv_error: OSError | None = None,
    ) -> None:
        self._request = request
        self._read = False
        self._fail_write = fail_write
        self._recv_error = recv_error
        self.written: list[bytes] = []
        self.closed = False

    def getsockopt(self, level: int, option: int, size: int) -> bytes:
        return struct.pack("3i", os.getpid(), os.getuid(), os.getgid())

    def recv(self, size: int) -> bytes:
        if self._recv_error is not None:
            raise self._recv_error
        if self._read:
            return b""
        self._read = True
        return self._request

    def sendall(self, data: bytes) -> None:
        if self._fail_write:
            raise BrokenPipeError("peer went away before the response")
        self.written.append(data)

    def close(self) -> None:
        self.closed = True


class _FakeSession:
    """A session-shaped stand-in whose serve path is the real transport."""

    def __init__(self, repo: Path | None = None) -> None:
        self.repo = repo or Path("/tmp/fake-repo")
        self.job_id = 1
        self.ledger = None
        self.closed = False

    def serve(self, endpoint: object, conn: object) -> dict:
        return broker_client.serve_one(endpoint, conn)

    def close(self) -> None:
        self.closed = True


# ---------------------------------------------------------------------------
# A. Operator principal provisioning
# ---------------------------------------------------------------------------


class OperatorPrincipalProvisioningTests(unittest.TestCase):
    """The operator endpoint resolves a provisioned, distinct operator."""

    def test_default_operator_is_the_invoking_user(self) -> None:
        with mock.patch.object(
            authority_mod, "_current_user_name", return_value="alice"
        ):
            principals = authority_mod.principal_set_from_environment(
                env={}, resolver=lambda name: {"alice": 1000}.get(name)
            )
        self.assertEqual(principals.operator.name, "alice")
        self.assertEqual(principals.operator.uid, 1000)

    def test_provisioned_operator_env_overrides_the_invoking_user(self) -> None:
        env = {
            authority_mod.OPERATOR_PRINCIPAL_ENV: "brian",
            authority_mod.SERVICE_PRINCIPAL_ENV: "svc",
            authority_mod.WORKER_PRINCIPAL_ENV: "wrk",
        }
        with mock.patch.object(
            authority_mod, "_current_user_name", return_value="alice"
        ):
            principals = authority_mod.principal_set_from_environment(
                env=env,
                resolver=lambda name: {
                    "alice": 1000,
                    "brian": 1001,
                    "svc": 1002,
                    "wrk": 1003,
                }.get(name),
            )
        self.assertEqual(principals.operator.name, "brian")
        self.assertEqual(principals.operator.uid, 1001)
        self.assertTrue(principals.distinct)

    def test_explicit_operator_name_wins_over_the_env(self) -> None:
        env = {authority_mod.OPERATOR_PRINCIPAL_ENV: "brian"}
        principals = authority_mod.principal_set_from_environment(
            env=env,
            operator_name="carol",
            resolver=lambda name: {"brian": 1001, "carol": 1004}.get(name),
        )
        self.assertEqual(principals.operator.name, "carol")

    def test_blank_operator_env_is_ignored(self) -> None:
        with mock.patch.object(
            authority_mod, "_current_user_name", return_value="alice"
        ):
            principals = authority_mod.principal_set_from_environment(
                env={authority_mod.OPERATOR_PRINCIPAL_ENV: "  "},
                resolver=lambda name: {"alice": 1000}.get(name),
            )
        self.assertEqual(principals.operator.name, "alice")

    def _resolve_with(self, mapping: dict[str, int]):
        def resolve(role: str, name: str, **kwargs: object):
            return authority_mod.Principal(
                role=role, name=name, uid=mapping.get(name)
            )

        return mock.patch.object(
            authority_mod, "resolve_principal", side_effect=resolve
        )

    def test_operator_allowlist_uses_the_provisioned_principal(self) -> None:
        env = {authority_mod.OPERATOR_PRINCIPAL_ENV: "brian"}
        with self._resolve_with({"brian": 1000, "opsx-supervisor": 1001}):
            allowed = supervision_mod._operator_allowed_uids(
                env, service_uid=1002, worker_uid=1003
            )
        self.assertEqual(allowed, frozenset({1000}))

    def test_serving_as_the_service_without_an_operator_fails_closed(self) -> None:
        with self._resolve_with({"brian": 1000}):
            with self.assertRaises(broker_mod.BrokerUnavailableError) as ctx:
                supervision_mod._operator_allowed_uids(
                    {}, service_uid=os.getuid()
                )
        self.assertIn(
            authority_mod.OPERATOR_PRINCIPAL_ENV, str(ctx.exception)
        )

    def test_ordinary_operator_cli_keeps_the_invoking_user(self) -> None:
        with mock.patch.object(
            authority_mod, "_current_user_name", return_value="alice"
        ), self._resolve_with({"alice": 1000}):
            allowed = supervision_mod._operator_allowed_uids(
                {}, service_uid=4321
            )
        self.assertEqual(allowed, frozenset({1000}))

    def test_operator_allowlist_excludes_the_service_and_worker(self) -> None:
        env = {authority_mod.OPERATOR_PRINCIPAL_ENV: "brian"}
        with self._resolve_with({"brian": 1002}):
            with self.assertRaises(broker_mod.BrokerUnavailableError) as ctx:
                supervision_mod._operator_allowed_uids(
                    env, service_uid=1001, worker_uid=1002
                )
        self.assertIn("worker principal", str(ctx.exception))
        with self._resolve_with({"brian": 1001}):
            with self.assertRaises(broker_mod.BrokerUnavailableError) as ctx:
                supervision_mod._operator_allowed_uids(
                    env, service_uid=1001, worker_uid=1002
                )
        self.assertIn("service principal", str(ctx.exception))

    def test_unresolvable_provisioned_operator_fails_closed(self) -> None:
        env = {authority_mod.OPERATOR_PRINCIPAL_ENV: "ghost"}
        with self._resolve_with({}):
            with self.assertRaises(broker_mod.BrokerUnavailableError) as ctx:
                supervision_mod._operator_allowed_uids(
                    env, service_uid=1001, worker_uid=1002
                )
        self.assertIn("does not exist", str(ctx.exception))


# ---------------------------------------------------------------------------
# A/B. Production host bootstrap resolution
# ---------------------------------------------------------------------------


class HostProvisioningTests(unittest.TestCase):
    """``open_service_host`` resolves distinct endpoint allowlists and groups."""

    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.root = Path(self._tmp.name)
        self.repo = self.root / "repo"
        self.repo.mkdir()
        self.store = self.root / "supervisor.sqlite3"
        self.env = {
            supervision_mod.OPERATOR_SOCKET_ENV: str(self.root / "operator.sock"),
            supervision_mod.WORKER_SOCKET_ENV: str(self.root / "worker.sock"),
            authority_mod.OPERATOR_PRINCIPAL_ENV: _current_user_name(),
        }

    def _boot(self, **kwargs):
        service_uid = kwargs.pop("service_uid", 1001)
        worker_uid = kwargs.pop("worker_uid", 1002)
        worker_name = kwargs.pop("worker_name", "opsx-worker")
        env = kwargs.pop("env", self.env)
        session = _FakeSession(self.repo)
        # Resolve the operator principal from an injected account table so the
        # test does not depend on host provisioning.
        accounts = {
            _current_user_name(): os.getuid(),
            "opsx-worker": 1002,
        }

        def resolve(role: str, name: str, **rest: object):
            return authority_mod.Principal(
                role=role, name=name, uid=accounts.get(name)
            )

        with mock.patch.object(
            authority_mod, "resolve_principal", side_effect=resolve
        ), mock.patch.object(
            supervision_mod, "open_service_session", return_value=session
        ):
            host = supervision_mod.open_service_host(
                self.repo,
                {},
                store_path=self.store,
                service_principal=SimpleNamespace(uid=service_uid),
                worker_principal=SimpleNamespace(uid=worker_uid, name=worker_name),
                env=env,
                **kwargs,
            )
        self.addCleanup(host.close)
        return host, session

    def test_operator_endpoint_accepts_only_the_operator(self) -> None:
        host, _session = self._boot()
        self.assertEqual(host.operator_allowed_uids, frozenset({os.getuid()}))

    def test_worker_endpoint_accepts_service_and_worker_never_operator(self) -> None:
        host, _session = self._boot()
        self.assertEqual(host.worker_allowed_uids, frozenset({1001, 1002}))
        self.assertNotIn(os.getuid(), host.worker_allowed_uids)

    def test_worker_endpoint_includes_the_provisioned_worker(self) -> None:
        host, _session = self._boot()
        self.assertIn(1002, host.worker_allowed_uids)
        self.assertEqual(host.worker_uid, 1002)

    def test_missing_operator_principal_while_serving_fails_closed(self) -> None:
        env = {
            key: value
            for key, value in self.env.items()
            if key != authority_mod.OPERATOR_PRINCIPAL_ENV
        }
        session = _FakeSession(self.repo)
        with mock.patch.object(
            supervision_mod, "open_service_session", return_value=session
        ):
            with self.assertRaises(broker_mod.BrokerUnavailableError) as ctx:
                supervision_mod.open_service_host(
                    self.repo,
                    {},
                    store_path=self.store,
                    service_principal=SimpleNamespace(uid=os.getuid()),
                    worker_principal=SimpleNamespace(uid=1002, name="opsx-worker"),
                    env=env,
                )
        self.assertIn("not accepted as the operator", str(ctx.exception))
        self.assertTrue(session.closed, "a failed boot must tear the session down")

    def test_operator_collapsed_with_the_worker_fails_closed(self) -> None:
        env = dict(self.env)
        env[authority_mod.OPERATOR_PRINCIPAL_ENV] = _current_user_name()
        with self.assertRaises(broker_mod.BrokerUnavailableError) as ctx:
            self._boot(
                env=env,
                worker_uid=os.getuid(),
                worker_name=_current_user_name(),
            )
        self.assertIn("also the worker principal", str(ctx.exception))

    def test_collapsed_service_and_worker_fails_closed(self) -> None:
        with self.assertRaises(broker_mod.BrokerUnavailableError) as ctx:
            self._boot(service_uid=1001, worker_uid=1001)
        self.assertIn("collapsed configuration", str(ctx.exception))

    def test_configured_but_missing_group_fails_closed(self) -> None:
        env = dict(self.env)
        env[supervision_mod.OPERATOR_GROUP_ENV] = "opsx-no-such-group-xyz"
        with self.assertRaises(broker_mod.BrokerUnavailableError) as ctx:
            self._boot(env=env)
        self.assertIn("does not exist", str(ctx.exception))

    def test_operator_group_that_admits_the_worker_fails_closed(self) -> None:
        env = dict(self.env)
        env[supervision_mod.OPERATOR_GROUP_ENV] = "opsx-operators"
        with mock.patch.object(
            supervision_mod, "_resolve_provisioned_group", return_value=4242
        ), mock.patch.object(
            supervision_mod, "_group_admits_principal", return_value=True
        ):
            with self.assertRaises(broker_mod.BrokerUnavailableError) as ctx:
                self._boot(env=env)
        self.assertIn("admits the worker principal", str(ctx.exception))

    def test_operator_group_excluding_the_worker_is_accepted(self) -> None:
        env = dict(self.env)
        env[supervision_mod.OPERATOR_GROUP_ENV] = "opsx-operators"
        env[supervision_mod.WORKER_GROUP_ENV] = "opsx-workers"
        with mock.patch.object(
            supervision_mod, "_resolve_provisioned_group", return_value=4242
        ), mock.patch.object(
            supervision_mod, "_group_admits_principal", return_value=False
        ):
            host, _session = self._boot(env=env)
        self.assertEqual(host.operator_socket_gid, 4242)
        self.assertEqual(host.worker_socket_gid, 4242)

    def test_no_configured_group_keeps_owner_only_sockets(self) -> None:
        host, _session = self._boot()
        self.assertIsNone(host.operator_socket_gid)
        self.assertIsNone(host.worker_socket_gid)

    def test_service_socket_path_honors_the_env_mapping(self) -> None:
        store = self.root / "store.sqlite3"
        env = {
            supervision_mod.OPERATOR_SOCKET_ENV: str(self.root / "op.sock"),
        }
        self.assertEqual(
            supervision_mod.service_socket_path(
                endpoints_mod.ENDPOINT_OPERATOR, store, env=env
            ),
            self.root / "op.sock",
        )
        self.assertEqual(
            supervision_mod.service_socket_path(
                endpoints_mod.ENDPOINT_OPERATOR, store, env={}
            ),
            store.parent / "operator.sock",
        )

    def test_group_admission_checks_primary_gid_and_member_list(self) -> None:
        primary_gid = pwd.getpwuid(os.getuid()).pw_gid
        self.assertTrue(
            supervision_mod._group_admits_principal(
                primary_gid, name=_current_user_name(), uid=os.getuid()
            )
        )
        self.assertFalse(
            supervision_mod._group_admits_principal(
                2**31 - 1, name="nobody-in-this-group-xyz", uid=None
            )
        )

    def test_operator_group_assertion_names_the_worker(self) -> None:
        with mock.patch.object(
            supervision_mod, "_group_admits_principal", return_value=True
        ):
            with self.assertRaises(broker_mod.BrokerUnavailableError) as ctx:
                supervision_mod._assert_operator_group_excludes_worker(
                    4242, name="opsx-worker", uid=1002
                )
        self.assertIn("opsx-worker", str(ctx.exception))


# ---------------------------------------------------------------------------
# B. Socket permissions and secure parents
# ---------------------------------------------------------------------------


class SocketProvisioningTests(unittest.TestCase):
    """Socket groups and parent-directory validation fail closed when unsafe."""

    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.root = Path(self._tmp.name)

    def _host(self, **overrides):
        kwargs = dict(
            session=_FakeSession(),
            store_path=self.root / "store.sqlite3",
            operator_allowed_uids=frozenset({os.getuid()}),
            worker_allowed_uids=frozenset({os.getuid()}),
            operator_socket=self.root / "operator.sock",
            worker_socket=self.root / "worker.sock",
            service_uid=os.getuid(),
        )
        kwargs.update(overrides)
        host = supervision_mod.ServiceEndpointHost(**kwargs)
        self.addCleanup(host.close)
        return host

    def test_group_socket_is_service_owned_0660(self) -> None:
        gid = os.getgid()
        host = self._host(operator_socket_gid=gid)
        host.bind()
        result = os.stat(self.root / "operator.sock")
        self.assertEqual(stat.S_IMODE(result.st_mode), 0o660)
        self.assertEqual(result.st_gid, gid)
        self.assertEqual(result.st_uid, os.getuid())

    def test_ungrouped_socket_stays_owner_only(self) -> None:
        host = self._host()
        host.bind()
        result = os.stat(self.root / "operator.sock")
        self.assertEqual(stat.S_IMODE(result.st_mode), 0o600)

    def test_socket_group_does_not_bypass_peer_uid_authentication(self) -> None:
        # The socket is reachable through the caller's own group, but no uid
        # is in the operator allowlist: the kernel-checked uid decides.
        host = self._host(
            operator_allowed_uids=frozenset(),
            operator_socket_gid=os.getgid(),
        )
        host.bind()
        outcome: dict = {}

        def client() -> None:
            try:
                broker_client.call(
                    {"verb": "enable"},
                    kind=endpoints_mod.ENDPOINT_OPERATOR,
                    socket_path=host.operator_socket,
                )
            except broker_mod.BrokerError as exc:
                outcome["error"] = type(exc).__name__

        thread = threading.Thread(target=client)
        thread.start()
        result = host.poll(timeout=10)
        thread.join(timeout=10)
        self.assertFalse(thread.is_alive())
        self.assertFalse(result["ok"])
        self.assertEqual(result["error"], "PeerCredentialError")
        self.assertEqual(outcome.get("error"), "BrokerMediationError")

    def test_world_writable_parent_fails_closed(self) -> None:
        shared = self.root / "shared"
        shared.mkdir()
        os.chmod(shared, 0o777)
        host = self._host(
            operator_socket=shared / "operator.sock",
            worker_socket=shared / "worker.sock",
        )
        with self.assertRaises(broker_mod.BrokerUnavailableError) as ctx:
            host.bind()
        self.assertIn("world-writable", str(ctx.exception))
        self.assertFalse((shared / "operator.sock").exists())

    def test_worker_writable_parent_fails_closed(self) -> None:
        denied = authority_mod.WriteDecision(denied=False, reason="test grant")
        host = self._host(worker_uid=os.getuid() + 4242)
        with mock.patch.object(
            authority_mod, "worker_write_denied", return_value=denied
        ):
            with self.assertRaises(broker_mod.BrokerUnavailableError) as ctx:
                host.bind()
        self.assertIn("writable by the worker principal", str(ctx.exception))

    def test_untrusted_parent_owner_fails_closed(self) -> None:
        base = os.stat(self.root)
        fabricated = os.stat_result(
            (stat.S_IFDIR | 0o755, base.st_ino, base.st_dev, 1, 12345, 12345, 0, 0, 0, 0)
        )
        reasons = supervision_mod._socket_parent_reasons(
            fabricated, service_uid=1000, worker_uid=None
        )
        self.assertEqual(len(reasons), 1)
        self.assertIn("owned by uid 12345", reasons[0])

    def test_secure_parent_passes_with_worker_write_denied(self) -> None:
        base = os.stat(self.root)
        fabricated = os.stat_result(
            (stat.S_IFDIR | 0o750, base.st_ino, base.st_dev, 1, 1001, 1001, 0, 0, 0, 0)
        )
        reasons = supervision_mod._socket_parent_reasons(
            fabricated,
            service_uid=1001,
            worker_uid=1002,
            worker_denied_check=lambda _stat: True,
        )
        self.assertEqual(reasons, [])

    def test_non_directory_parent_is_named(self) -> None:
        base = os.stat(self.root)
        fabricated = os.stat_result(
            (stat.S_IFREG | 0o600, base.st_ino, base.st_dev, 1, 1001, 1001, 0, 0, 0, 0)
        )
        reasons = supervision_mod._socket_parent_reasons(
            fabricated, service_uid=1001, worker_uid=None
        )
        self.assertEqual(reasons, ["it is not a directory"])

    def test_existing_non_socket_path_is_not_removed(self) -> None:
        planted = self.root / "operator.sock"
        planted.write_text("not a socket\n", encoding="utf-8")
        host = self._host()
        with self.assertRaises(broker_mod.BrokerUnavailableError) as ctx:
            host.bind()
        self.assertIn("not a socket", str(ctx.exception))
        self.assertTrue(planted.exists())

    def test_stale_socket_is_replaced(self) -> None:
        stale = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        stale.bind(str(self.root / "operator.sock"))
        stale.close()
        host = self._host()
        host.bind()
        self.assertTrue((self.root / "operator.sock").is_socket())


# ---------------------------------------------------------------------------
# C. Per-connection fault containment
# ---------------------------------------------------------------------------


class ConnectionFaultContainmentTests(unittest.TestCase):
    """One bad client cannot tear down the serve loop or fake a delivery."""

    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.root = Path(self._tmp.name)
        self.endpoint = endpoints_mod.Endpoint(
            endpoints_mod.ENDPOINT_OPERATOR, frozenset({os.getuid()})
        )

    def _raw_client(self, payload: bytes) -> tuple[threading.Thread, dict]:
        outcome: dict = {}

        def client() -> None:
            conn = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
            conn.settimeout(10)
            try:
                conn.connect(str(self.root / "operator.sock"))
                conn.sendall(payload)
                outcome["response"] = conn.recv(4096)
            except OSError as exc:  # pragma: no cover - failure probe
                outcome["transport_error"] = str(exc)
            finally:
                conn.close()

        thread = threading.Thread(target=client)
        return thread, outcome

    def _bound_host(self, **overrides):
        kwargs = dict(
            session=_FakeSession(self.root),
            store_path=self.root / "store.sqlite3",
            operator_allowed_uids=frozenset({os.getuid()}),
            worker_allowed_uids=frozenset({os.getuid()}),
            operator_socket=self.root / "operator.sock",
            worker_socket=self.root / "worker.sock",
            service_uid=os.getuid(),
        )
        kwargs.update(overrides)
        host = supervision_mod.ServiceEndpointHost(**kwargs).bind()
        self.addCleanup(host.close)
        return host

    def test_malformed_json_is_answered_and_the_loop_continues(self) -> None:
        host = self._bound_host()
        thread, outcome = self._raw_client(b"this is not json\n")
        thread.start()
        result = host.poll(timeout=10)
        thread.join(timeout=10)
        self.assertFalse(thread.is_alive())
        self.assertEqual(result["ok"], False)
        self.assertEqual(result["error"], "EndpointError")
        response = json.loads(outcome["response"])
        self.assertFalse(response["ok"])
        self.assertEqual(response["error"], "EndpointError")

        # A second connection is still served: an authority refusal travels
        # back as its named error instead of the loop dying with the first.
        second: dict = {}

        def client() -> None:
            try:
                broker_client.call(
                    {"verb": "approve", "change_ids": ["x"]},
                    kind=endpoints_mod.ENDPOINT_OPERATOR,
                    socket_path=host.operator_socket,
                )
            except broker_mod.BrokerError as exc:
                second["error"] = type(exc).__name__

        thread2 = threading.Thread(target=client)
        thread2.start()
        result2 = host.poll(timeout=10)
        thread2.join(timeout=10)
        self.assertFalse(thread2.is_alive())
        self.assertEqual(result2["error"], "BrokerUnavailableError")
        self.assertEqual(second.get("error"), "BrokerUnavailableError")

    def test_mid_request_disconnect_is_contained(self) -> None:
        host = self._bound_host()
        thread, outcome = self._raw_client(b'{"verb": "approve"')
        thread.start()
        result = host.poll(timeout=10)
        thread.join(timeout=10)
        self.assertFalse(thread.is_alive())
        self.assertEqual(result["ok"], False)
        self.assertIn(
            result["error"], {"BrokerMediationError", "BrokenPipeError"}
        )
        # The serve loop is still healthy for the next connection.
        thread2, _ = self._raw_client(b"still not json\n")
        thread2.start()
        result2 = host.poll(timeout=10)
        thread2.join(timeout=10)
        self.assertEqual(result2["error"], "EndpointError")

    def test_connection_reset_during_read_is_contained(self) -> None:
        conn = _FakeConn(b"", recv_error=ConnectionResetError("peer reset"))
        result = broker_client.serve_one(self.endpoint, conn)
        self.assertEqual(result["ok"], False)
        self.assertEqual(result["error"], "BrokerMediationError")
        self.assertTrue(conn.closed)

    def test_failed_refusal_write_is_contained(self) -> None:
        conn = _FakeConn(b"not json\n", fail_write=True)
        result = broker_client.serve_one(self.endpoint, conn)
        self.assertEqual(result["error"], "EndpointError")

    def test_ledger_fault_is_not_silenced(self) -> None:
        conn = _FakeConn(
            broker_client.encode_request({"verb": "approve", "change_ids": ["x"]})
        )

        def resolver(_request: object) -> tuple:
            raise ledger_mod.LedgerError("store is unreadable")

        with self.assertRaises(ledger_mod.LedgerError):
            broker_client.serve_one(self.endpoint, conn, ledger_resolver=resolver)
        response = json.loads(conn.written[-1])
        self.assertFalse(response["ok"])
        self.assertEqual(response["error"], "LedgerError")

    def test_failed_response_write_does_not_acknowledge_the_delivery(self) -> None:
        acknowledged: list[bool] = []

        def handler(_request: object, _credentials: object):
            return endpoints_mod.DeferredDelivery(
                result={"verb": "deferred"},
                acknowledge=lambda: acknowledged.append(True),
            )

        surface = dict(endpoints_mod.DISPATCH_TABLES)
        surface[endpoints_mod.ENDPOINT_OPERATOR] = {"defer": handler}
        conn = _FakeConn(
            broker_client.encode_request({"verb": "defer"}), fail_write=True
        )
        with mock.patch.object(endpoints_mod, "DISPATCH_TABLES", surface):
            with self.assertRaises(OSError):
                broker_client.serve_one(self.endpoint, conn)
        self.assertEqual(acknowledged, [])


# ---------------------------------------------------------------------------
# D. Canonical supervision skill frontmatter
# ---------------------------------------------------------------------------


class SupervisionSkillFrontmatterTests(unittest.TestCase):
    """The canonical skill's frontmatter is valid YAML with an intact body."""

    def _frontmatter_lines(self) -> list[str]:
        text = SKILL_PATH.read_text(encoding="utf-8")
        self.assertTrue(text.startswith("---\n"))
        return text.split("---", 2)[1].strip("\n").splitlines()

    def test_description_is_a_folded_or_quoted_scalar(self) -> None:
        description_lines = [
            line
            for line in self._frontmatter_lines()
            if line.startswith("description:")
        ]
        self.assertEqual(len(description_lines), 1)
        value = description_lines[0].split(":", 1)[1].strip()
        self.assertTrue(
            value.startswith((">", "|", "'", '"')),
            f"description must use an explicit scalar, found {value!r}",
        )

    def test_frontmatter_parses_and_names_the_skill(self) -> None:
        if yaml is None:  # pragma: no cover - depends on the runner
            self.skipTest("PyYAML is not installed")
        data = yaml.safe_load("\n".join(self._frontmatter_lines()))
        self.assertIsInstance(data, dict)
        self.assertEqual(data["name"], "opsx-supervision")
        self.assertIn("supervised", data["description"])
        self.assertTrue(data["description"].startswith("Briefs the supervised"))

    def test_body_is_unchanged_by_the_frontmatter_fix(self) -> None:
        text = SKILL_PATH.read_text(encoding="utf-8")
        body = text.split("---", 2)[2]
        self.assertIn("# opsx-supervision — the primary's bounded loop", body)
        self.assertIn("You are the **frontier primary**", body)
        self.assertIn("opsx-supervise", body)


if __name__ == "__main__":  # pragma: no cover - unittest entry point
    unittest.main()
