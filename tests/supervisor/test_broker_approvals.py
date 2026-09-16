"""Broker-mediated approval tests (change ``enforce-broker-mediated-approvals``).

Covers the broker test suite tasks:

- 7.1 a worker-domain subprocess cannot approve, reset, or run in a registered
  job (conditional real restricted-process path, plus the structural checks);
- 7.2 a stale material revision does not satisfy a gate while unrelated updates
  do not invalidate a receipt;
- 7.3 ``approve --all`` / ``approve P<N>`` / ``accept`` are broker mediated and
  print exactly the affected change IDs;
- 7.4 a worker that drops supervised fields remains registered and mediated;
- 7.5 receipts wake the owning job without the held execution lock, and a
  restart rescans above the high-water mark;
- 7.6 legacy unregistered jobs keep prior JSON handling with no broker
  dependency;
- 7.7 the projection follows broker state and direct JSON edits have no
  authority;
- 7.8 pause/steer requests are durable receipts bound to the checkpoint and
  material revision, wake the job without the execution lock, and are refused
  for worker-domain callers.

Also asserts the endpoint disjointness/execution-domain properties (task 3.3)
and the three ``pause_before_human_only`` flag semantics end to end (task 5.2).
"""

from __future__ import annotations

import argparse
import importlib.util
import io
import json
import os
import socket
import subprocess
import sys
import tempfile
import textwrap
import unittest
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path
from unittest import mock

from lib.orchestrator import state as state_mod
from lib.orchestrator import supervision
from lib.supervisor import authority, broker, broker_client, endpoints, ledger, lock

REPO_ROOT = Path(__file__).resolve().parents[2]
SCRIPT = REPO_ROOT / "orchestrator" / "opsx-plan.py"

_STAGE_ENV = {
    "OPSX_CONTROLLER_MODEL": "test-provider/test-controller",
    "OPSX_IMPLEMENTER_MODEL": "test-provider/test-implementer",
    "OPSX_REVIEWER_MODEL": "test-provider/test-reviewer",
    "OPSX_ARCHIVER_MODEL": "test-provider/test-archiver",
}


def load_opsx_plan():
    spec = importlib.util.spec_from_file_location("opsx_plan", SCRIPT)
    assert spec is not None
    assert spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules["opsx_plan"] = module
    spec.loader.exec_module(module)
    return module


def git(repo: Path, *args: str) -> None:
    subprocess.run(["git", *args], cwd=repo, check=True, capture_output=True, text=True)


def _policy() -> dict:
    return {
        "authority_config": {"mode": "policy-bound"},
        "model_selection": {
            "version": 1,
            "roles": {"implementer": "cheap/model-a"},
            "stages": {"implement": "implementer"},
        },
        "inexpensive_allowlist": {
            "version": 1,
            "models": ["cheap/model-a"],
            "source": "test fixture",
        },
        "manifest_snapshot_hash": "placeholder",
        "budgets": {
            "version": 1,
            "total_cost_usd": 1.0,
            "per_action_cost_usd": None,
            "total_elapsed_minutes": None,
            "per_action_elapsed_minutes": None,
            "max_incident_attempts": None,
        },
        "deadlines": {"version": 1, "execution_deadline_minutes": None},
    }


def _manifest(*entries: str, review_created: bool = False) -> str:
    body = "\n".join(entries)
    return (
        "[plan]\n"
        'name = "broker-test"\n'
        'adapter = "opencode"\n'
        'created_check = ""\n'
        f"review_created = {'true' if review_created else 'false'}\n\n"
        f"{body}\n"
    )


HUMAN_GATED = (
    "[[changes]]\n"
    'id = "gated-human"\n'
    "phase = 1\n"
    "pause_before = true\n"
)
DELEGATED_GATED = (
    "[[changes]]\n"
    'id = "gated-delegated"\n'
    "phase = 2\n"
    "pause_before = true\n"
    "pause_before_human_only = false\n"
)
UNGATED = "[[changes]]\n" 'id = "open-change"\n' "phase = 3\n"


class BrokerTestCase(unittest.TestCase):
    """Shared temp layout: a git worktree and trusted ledger storage."""

    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.root = Path(self._tmp.name)
        self.repo = self.root / "repo"
        self.repo.mkdir()
        git(self.repo, "init")
        (self.repo / "tracked.txt").write_text("base\n", encoding="utf-8")
        git(self.repo, "add", "tracked.txt")
        git(
            self.repo,
            "-c", "user.email=test@example.invalid",
            "-c", "user.name=Test User",
            "commit", "-m", "init",
        )
        self.storage = self.root / "service-storage"
        self.storage.mkdir()
        self.db_path = self.storage / "supervisor.sqlite3"
        self.ledger = ledger.open_ledger(self.db_path, repository_root=self.repo)
        self.addCleanup(self.ledger.close)

        self.cid = "gated-human"
        self.cfg = {
            "name": "broker-test",
            "adapter": "opencode",
            "max_rounds": 2,
            "review_created": False,
            "created_check": "",
            "changes": {
                "gated-human": {
                    "id": "gated-human", "phase": 1, "depends_on": [],
                    "enabled": True, "pause_before": True,
                },
                "gated-delegated": {
                    "id": "gated-delegated", "phase": 2, "depends_on": [],
                    "enabled": True, "pause_before": True,
                },
                "open-change": {
                    "id": "open-change", "phase": 3,
                    "depends_on": ["gated-human"], "enabled": True,
                    "pause_before": False,
                },
            },
            "order": ["gated-human", "gated-delegated", "open-change"],
        }
        self.state = {"plan": "broker-test", "approvals": [], "changes": {}}
        # The trusted service installs the projection writer at boot. Most
        # tests exercise the broker directly, so install the same writer the
        # service would; tests that assert the fail-closed path clear it.
        self._projection_calls: list[tuple[Any, int]] = []
        self._install_recording_projection_writer()

    def _install_recording_projection_writer(self) -> None:
        """Install the service projection writer, recording each invocation.

        This mirrors what the supervised service does at boot via
        ``supervision.install_projection_writer``, with the extra bookkeeping
        the regressions assert.
        """
        def writer(conn: Any, job_id: int) -> None:
            self._projection_calls.append((conn, job_id))
            state = state_mod.load_state(self.repo, self.cfg["name"])
            supervision.persist_projection(self.repo, self.cfg, state, conn, job_id)

        self.addCleanup(broker.set_projection_writer, broker.projection_writer())
        broker.set_projection_writer(writer)

    # -- fixtures ---------------------------------------------------------

    def register(self, *, content: str | None = None, **overrides) -> int:
        params = {
            "run_id": "run-1",
            "worktree": self.repo,
            "owner": "service",
            "policy": _policy(),
            "operator": "operator",
            "manifest_content": content if content is not None else _manifest(
                HUMAN_GATED, DELEGATED_GATED, UNGATED
            ),
        }
        params.update(overrides)
        return self.ledger.register_job(**params)

    def _write_plan(self) -> Path:
        plan = self.repo / "openspec" / "plans" / "broker-test.toml"
        plan.parent.mkdir(parents=True, exist_ok=True)
        plan.write_text(
            _manifest(HUMAN_GATED, DELEGATED_GATED, UNGATED), encoding="utf-8"
        )
        return plan

    def enable_env(self, *, ledger: bool = True) -> None:
        env = dict(os.environ)
        if ledger:
            env["OPSX_SUPERVISOR_STATE_FILE"] = str(self.db_path)
        else:
            # No explicit backend and a temp HOME so the default ledger path
            # cannot accidentally exist.
            env.pop("OPSX_SUPERVISOR_STATE_FILE", None)
            env["HOME"] = str(self.root / "no-supervisor-home")
        env.pop(broker_client.OPERATOR_SOCKET_ENV, None)
        env.pop(broker_client.WORKER_SOCKET_ENV, None)
        patcher = mock.patch.dict(os.environ, env, clear=True)
        patcher.start()
        self.addCleanup(patcher.stop)

    def _fake_transport(self, job_id: int):
        """Return a transport that dispatches directly against the ledger.

        This mirrors the server half without a socket: the request carries only
        the verb and change ids, and the test injects the service-owned ledger
        exactly as the endpoint's resolver does.
        """
        calls: list[dict] = []

        def transport(request, *, kind):
            calls.append({"request": dict(request), "kind": kind})
            verb = request["verb"]
            principal = (
                broker.BrokerPrincipal(broker.OPERATOR, "operator", 1000)
                if kind == endpoints.ENDPOINT_OPERATOR
                else broker.BrokerPrincipal(
                    broker.SERVICE, request.get("service_identity", "opsx-supervisor"),
                    1001,
                )
            )
            change_ids = request.get("change_ids")
            if verb == "approve":
                recorded = broker.record_approval(
                    self.ledger, job_id, principal=principal, change_ids=change_ids
                )
                return {"approved": [r.change_id for r in recorded]}
            if verb == "accept":
                recorded = broker.record_acceptance(
                    self.ledger, job_id, principal=principal, change_ids=change_ids
                )
                return {"approved": [r.change_id for r in recorded]}
            if verb == "reset_change":
                recorded = [
                    broker.reset_change(
                        self.ledger, job_id, principal=principal, change_id=cid
                    )
                    for cid in change_ids
                ]
                return {"reset": [r.change_id for r in recorded]}
            if verb == "release_delegated_gate":
                receipt = broker.release_delegated_gate(
                    self.ledger, job_id, principal=principal,
                    change_id=request["change_id"],
                )
                return {"released": receipt.change_id}
            raise AssertionError(f"unexpected verb {verb}")

        return transport, calls

    def operator(self) -> broker.BrokerPrincipal:
        return broker.BrokerPrincipal(broker.OPERATOR, "alice", 1000)

    def service(self, name: str = "opsx-supervisor") -> broker.BrokerPrincipal:
        return broker.BrokerPrincipal(broker.SERVICE, name, 1001)

    def worker(self) -> broker.BrokerPrincipal:
        return broker.BrokerPrincipal(broker.WORKER, "opsx-worker", 1002)


# ---------------------------------------------------------------------------
# 2.x / 3.3: broker authority and endpoint disjointness
# ---------------------------------------------------------------------------


class BrokerAuthorityTests(BrokerTestCase):
    def test_operator_approval_records_a_bound_receipt(self) -> None:
        job_id = self.register()
        recorded = broker.record_approval(
            self.ledger, job_id, principal=self.operator(),
            change_ids=["gated-human"],
        )
        self.assertEqual(len(recorded), 1)
        row = self.ledger.get_receipt(recorded[0].receipt_id)
        self.assertEqual(row["kind"], "approval")
        self.assertEqual(row["authority"], "operator")
        self.assertEqual(row["checkpoint"], "approval:gated-human")
        state = broker.material_state(self.ledger, job_id, "gated-human")
        self.assertEqual(
            row["material_hash"],
            broker.material_hash(
                "gated-human", state.fields, state.snapshot_hash,
                state.policy_revision,
            ),
        )
        self.assertTrue(broker.is_dispatchable(self.ledger, job_id, "gated-human"))

    def test_worker_principal_cannot_record_any_authority_receipt(self) -> None:
        job_id = self.register()
        with self.assertRaises(broker.BrokerMediationError):
            broker.record_approval(
                self.ledger, job_id, principal=self.worker(),
                change_ids=["gated-human"],
            )
        with self.assertRaises(broker.BrokerMediationError):
            broker.reset_change(
                self.ledger, job_id, principal=self.worker(),
                change_id="gated-human",
            )
        with self.assertRaises(broker.BrokerMediationError):
            broker.record_pause_or_steer(
                self.ledger, job_id, principal=self.worker(),
                change_id="gated-human", kind="pause",
            )
        self.assertEqual(self.ledger.receipt_high_water(job_id), 0)

    def test_delegated_gate_refuses_operator_and_accepts_scoped_service(self) -> None:
        job_id = self.register()
        with self.assertRaises(broker.BrokerMediationError):
            broker.record_approval(
                self.ledger, job_id, principal=self.operator(),
                change_ids=["gated-delegated"],
            )
        receipt = broker.release_delegated_gate(
            self.ledger, job_id, principal=self.service(),
            change_id="gated-delegated",
        )
        self.assertEqual(receipt.authority, "service")
        self.assertTrue(
            broker.is_dispatchable(self.ledger, job_id, "gated-delegated")
        )

    def test_invalid_protected_snapshot_flag_fails_closed(self) -> None:
        """A protected snapshot with human-only but no gate is rejected.

        ``gate_fields`` must apply the manifest loader's strict check to the
        protected snapshot, so the invalid combination is a named refusal rather
        than a normalization into an ungated change. It must fail closed on
        every path that reads the snapshot, including the gate resolver.
        """
        job_id = self.register(content=_manifest(
            '[[changes]]\nid = "gated-human"\nphase = 1\n'
            "pause_before = false\npause_before_human_only = true\n"
        ))
        snapshot = broker.load_protected_snapshot(self.ledger, job_id)
        with self.assertRaises(broker.BrokerError):
            broker.gate_fields(snapshot, "gated-human")
        with self.assertRaises(broker.BrokerError):
            broker.material_state(self.ledger, job_id, "gated-human")
        with self.assertRaises(broker.BrokerError):
            broker.resolve_gate(self.ledger, job_id, "gated-human")
        # The refusal records nothing: no receipt, no projection change.
        self.assertEqual(self.ledger.receipt_high_water(job_id), 0)
        self.assertEqual(self._projection_calls, [])

    def test_non_boolean_protected_snapshot_flag_fails_closed(self) -> None:
        job_id = self.register(content=_manifest(
            '[[changes]]\nid = "gated-human"\nphase = 1\n'
            "pause_before = true\npause_before_human_only = 1\n"
        ))
        snapshot = broker.load_protected_snapshot(self.ledger, job_id)
        with self.assertRaises(broker.BrokerError):
            broker.gate_fields(snapshot, "gated-human")

    def test_release_delegated_gate_registered_identity_is_enforced(self) -> None:
        job_id = self.register(owner_principal="opsx-supervisor")
        result = self._call_over_socketpair(
            job_id,
            {
                "verb": "release_delegated_gate",
                "change_id": "gated-delegated",
                "service_identity": "someone-else",
                "role": "implementer",
                "observed_agent": "opsx-implementer",
            },
            endpoint_kind=endpoints.ENDPOINT_WORKER,
        )
        self.assertFalse(result[0])
        self.assertEqual(result[1], "BrokerMediationError")

    def _call_over_socketpair(
        self, job_id: int, request: dict, *, endpoint_kind: str
    ) -> tuple[bool, str]:
        """Serve one request over a real socketpair and return (ok, detail).

        The server half runs on the main thread (the ledger connection is not
        shared across threads) and authenticates with the real peer
        credentials, injecting the service-owned ledger exactly as the endpoint
        resolver does; the client half uses the shared framing on a thread.
        """
        import threading

        server, client = socket.socketpair(socket.AF_UNIX, socket.SOCK_STREAM)
        self.addCleanup(server.close)
        self.addCleanup(client.close)
        real = endpoints.peer_credentials(server)
        endpoint = endpoints.Endpoint(endpoint_kind, frozenset({real.uid}))
        outcome: dict = {}

        def client_call() -> None:
            try:
                outcome["result"] = broker_client.call(
                    request, kind=endpoint_kind, connector=lambda: client
                )
            except broker.BrokerError as exc:
                outcome["error"] = type(exc).__name__

        thread = threading.Thread(target=client_call)
        thread.start()
        try:
            broker_client.serve_one(
                endpoint, server,
                ledger_resolver=lambda _req: (self.ledger, job_id),
            )
        finally:
            thread.join(timeout=10)
        self.assertFalse(thread.is_alive(), "client half did not finish")
        if "error" in outcome:
            return False, outcome["error"]
        return True, "ok"

    def test_worker_endpoint_has_no_approval_family_verb(self) -> None:
        worker_endpoint = endpoints.Endpoint(
            endpoints.ENDPOINT_WORKER, frozenset({1002})
        )
        for verb in ("approve", "accept", "reset", "reset_change"):
            with self.assertRaises(endpoints.EndpointError):
                worker_endpoint.resolve(verb)
        self.assertTrue(endpoints.handler_tables_are_disjoint())

    def test_dispatcher_executes_repo_code_still_holds(self) -> None:
        self.assertFalse(endpoints.dispatcher_executes_repo_code())
        self.assertFalse(
            endpoints.dispatcher_executes_repo_code(endpoints.DISPATCH_TABLES)
        )

    def test_unknown_change_in_snapshot_is_refused(self) -> None:
        job_id = self.register()
        with self.assertRaises(broker.BrokerError):
            broker.resolve_gate(self.ledger, job_id, "not-in-snapshot")


# ---------------------------------------------------------------------------
# Review regression: endpoint-recorded receipts regenerate the projection
# ---------------------------------------------------------------------------


class EndpointProjectionTests(BrokerTestCase):
    """Endpoint receipts must regenerate the JSON projection themselves.

    The trusted service installs the projection writer; endpoint handlers must
    not depend on callers supplying an optional callback, so driving the
    operator/worker verbs over the real socketpair surface updates the JSON
    projection as a side effect of the broker transaction.
    """

    def _json_approvals(self) -> list[str]:
        return state_mod.load_state(self.repo, self.cfg["name"])["approvals"]

    def _serve(self, job_id: int, request: dict, *, endpoint_kind: str) -> tuple[bool, str]:
        import threading

        server, client = socket.socketpair(socket.AF_UNIX, socket.SOCK_STREAM)
        self.addCleanup(server.close)
        self.addCleanup(client.close)
        real = endpoints.peer_credentials(server)
        endpoint = endpoints.Endpoint(endpoint_kind, frozenset({real.uid}))
        outcome: dict = {}

        def client_call() -> None:
            try:
                outcome["result"] = broker_client.call(
                    request, kind=endpoint_kind, connector=lambda: client
                )
            except broker.BrokerError as exc:
                outcome["error"] = type(exc).__name__

        thread = threading.Thread(target=client_call)
        thread.start()
        try:
            broker_client.serve_one(
                endpoint, server,
                ledger_resolver=lambda _req: (self.ledger, job_id),
            )
        finally:
            thread.join(timeout=10)
        self.assertFalse(thread.is_alive(), "client half did not finish")
        if "error" in outcome:
            return False, outcome["error"]
        return True, "ok"

    def test_operator_endpoint_approve_regenerates_projection(self) -> None:
        job_id = self.register()
        ok, detail = self._serve(
            job_id,
            {"verb": "approve", "change_ids": ["gated-human"]},
            endpoint_kind=endpoints.ENDPOINT_OPERATOR,
        )
        self.assertTrue(ok, detail)
        self.assertEqual(self.ledger.receipt_high_water(job_id), 1)
        # The projection followed broker state without any caller callback.
        self.assertEqual(self._json_approvals(), ["gated-human"])
        self.assertEqual(self._projection_calls, [(self.ledger, job_id)])

    def test_worker_endpoint_delegated_release_regenerates_projection(self) -> None:
        job_id = self.register(owner_principal="opsx-supervisor")
        ok, detail = self._serve(
            job_id,
            {
                "verb": "release_delegated_gate",
                "change_id": "gated-delegated",
                "service_identity": "opsx-supervisor",
                "role": "implementer",
                "observed_agent": "opsx-implementer",
            },
            endpoint_kind=endpoints.ENDPOINT_WORKER,
        )
        self.assertTrue(ok, detail)
        self.assertEqual(self.ledger.receipt_high_water(job_id), 1)
        # The delegated gate appears in the projection after the transaction.
        self.assertEqual(self._json_approvals(), ["gated-delegated"])
        self.assertEqual(self._projection_calls, [(self.ledger, job_id)])

    def test_operator_endpoint_reset_regenerates_projection(self) -> None:
        job_id = self.register()
        ok, detail = self._serve(
            job_id,
            {"verb": "reset_change", "change_ids": ["gated-human"]},
            endpoint_kind=endpoints.ENDPOINT_OPERATOR,
        )
        self.assertTrue(ok, detail)
        self.assertEqual(
            [row["kind"] for row in self.ledger.receipts_for_change(
                job_id, "gated-human")],
            ["reset"],
        )
        # The projection was regenerated from broker state after the reset.
        self.assertEqual(self._projection_calls, [(self.ledger, job_id)])
        # And it is internally consistent with the broker resolver.
        for cid in ("gated-human", "gated-delegated", "open-change"):
            in_projection = cid in self._json_approvals()
            resolution = broker.resolve_gate(self.ledger, job_id, cid)
            self.assertEqual(
                in_projection,
                resolution.authority is not None and resolution.dispatchable,
                f"projection disagrees with broker state for {cid}",
            )

    def test_endpoint_receipt_without_writer_fails_closed_and_records_nothing(self) -> None:
        job_id = self.register()
        broker.set_projection_writer(None)
        ok, detail = self._serve(
            job_id,
            {"verb": "approve", "change_ids": ["gated-human"]},
            endpoint_kind=endpoints.ENDPOINT_OPERATOR,
        )
        self.assertFalse(ok)
        self.assertEqual(detail, "BrokerUnavailableError")
        self.assertEqual(
            self.ledger.receipt_high_water(job_id), 0,
            "a receipt must not commit without a projection writer",
        )

    def test_service_boot_writer_regenerates_projection(self) -> None:
        """The service's own boot-time writer regenerates the projection."""
        job_id = self.register()
        # Replace the test's recording writer with the service installer.
        broker.set_projection_writer(None)
        previous = supervision.install_projection_writer(self.repo, self.cfg)
        self.addCleanup(broker.set_projection_writer, previous)
        ok, detail = self._serve(
            job_id,
            {"verb": "approve", "change_ids": ["gated-human"]},
            endpoint_kind=endpoints.ENDPOINT_OPERATOR,
        )
        self.assertTrue(ok, detail)
        self.assertEqual(
            state_mod.load_state(self.repo, self.cfg["name"])["approvals"],
            ["gated-human"],
        )


# ---------------------------------------------------------------------------
# Production bootstrap: the live service installs the writer before serving
# ---------------------------------------------------------------------------


class ProductionServiceBootstrapTests(BrokerTestCase):
    """The production service path installs the projection writer itself.

    Regression for the review finding that ``install_projection_writer`` had
    no production call site: only tests installed it, so live endpoint receipt
    requests failed closed instead of recording and projecting receipts. These
    tests exercise :func:`supervision.open_service_session` and
    :func:`supervision.open_service_host` — the real service bootstrap — with
    the test's convenience writer cleared, and assert both the durable receipt
    and the JSON projection updates through the endpoint.
    """

    def _clear_writer(self) -> None:
        """Clear the test-installed writer so only production code installs one."""
        broker.set_projection_writer(None)

    def _register_foreign_job(self) -> int:
        """Register a second active job for a *different* worktree in this store.

        The job belongs to another worktree under the same repository root, so
        an explicit ``--job-id`` naming it is a foreign job for ``self.repo``.
        """
        foreign = self.repo / "foreign-worktree"
        foreign.mkdir(exist_ok=True)
        return self.ledger.register_job(
            run_id="run-foreign",
            worktree=foreign,
            owner="service",
            policy=_policy(),
            operator="operator",
            manifest_content=_manifest(HUMAN_GATED, DELEGATED_GATED, UNGATED),
        )

    def setUp(self) -> None:
        # Save the writer the shared fixture installed, then clear it so a
        # passing test proves production code installed the writer itself.
        super().setUp()
        self._saved_writer = broker.projection_writer()
        broker.set_projection_writer(None)
        self.addCleanup(broker.set_projection_writer, self._saved_writer)

    def _serve_with_session(
        self, session, job_id: int, request: dict, *, endpoint_kind: str
    ) -> tuple[bool, str]:
        import threading

        server, client = socket.socketpair(socket.AF_UNIX, socket.SOCK_STREAM)
        self.addCleanup(server.close)
        self.addCleanup(client.close)
        real = endpoints.peer_credentials(server)
        endpoint = endpoints.Endpoint(endpoint_kind, frozenset({real.uid}))
        outcome: dict = {}

        def client_call() -> None:
            try:
                outcome["result"] = broker_client.call(
                    request, kind=endpoint_kind, connector=lambda: client
                )
            except broker.BrokerError as exc:
                outcome["error"] = type(exc).__name__

        thread = threading.Thread(target=client_call)
        thread.start()
        try:
            session.serve(endpoint, server)
        finally:
            thread.join(timeout=10)
        self.assertFalse(thread.is_alive(), "client half did not finish")
        if "error" in outcome:
            return False, outcome["error"]
        return True, "ok"

    def test_service_session_installs_writer_and_projects_operator_receipt(self) -> None:
        """Booting the service session alone makes approvals record and project."""
        self.assertIsNone(
            broker.projection_writer(),
            "the production bootstrap must be the thing that installs the writer",
        )
        job_id = self.register()
        with supervision.open_service_session(
            self.repo, self.cfg, store_path=self.db_path
        ) as session:
            self.assertIsNotNone(
                broker.projection_writer(),
                "open_service_session must install the trusted projection writer",
            )
            ok, detail = self._serve_with_session(
                session, job_id,
                {"verb": "approve", "change_ids": ["gated-human"]},
                endpoint_kind=endpoints.ENDPOINT_OPERATOR,
            )
        self.assertTrue(ok, detail)
        self.assertEqual(self.ledger.receipt_high_water(job_id), 1)
        self.assertEqual(
            state_mod.load_state(self.repo, self.cfg["name"])["approvals"],
            ["gated-human"],
        )
        # Closing the session restores the previous (cleared) writer.
        self.assertIsNone(broker.projection_writer())

    def test_service_session_projects_delegated_release(self) -> None:
        """The worker scoped action also records and projects via the session."""
        job_id = self.register(owner_principal="opsx-supervisor")
        with supervision.open_service_session(
            self.repo, self.cfg, store_path=self.db_path
        ) as session:
            ok, detail = self._serve_with_session(
                session, job_id,
                {
                    "verb": "release_delegated_gate",
                    "change_id": "gated-delegated",
                    "service_identity": "opsx-supervisor",
                    "role": "implementer",
                    "observed_agent": "opsx-implementer",
                },
                endpoint_kind=endpoints.ENDPOINT_WORKER,
            )
        self.assertTrue(ok, detail)
        self.assertEqual(self.ledger.receipt_high_water(job_id), 1)
        self.assertEqual(
            state_mod.load_state(self.repo, self.cfg["name"])["approvals"],
            ["gated-delegated"],
        )

    def test_service_session_without_registered_job_fails_closed(self) -> None:
        """A worktree with no active job cannot open a broker session."""
        self._clear_writer()
        with self.assertRaises(broker.BrokerUnavailableError):
            supervision.open_service_session(
                self.repo, self.cfg, store_path=self.db_path
            )

    def test_service_session_rejects_foreign_explicit_job_id(self) -> None:
        """An explicit --job-id for another worktree is refused before serving.

        Regression for the review finding that ``open_service_session`` accepted
        any existing ledger job: a foreign job id must be rejected as a
        worktree/registration mismatch before the projection writer is
        installed, so no receipt or projection change can occur for it.
        """
        self._clear_writer()
        own_job = self.register()
        foreign_job = self._register_foreign_job()
        self.assertNotEqual(own_job, foreign_job)
        state = state_mod.load_state(self.repo, self.cfg["name"])
        before = json.dumps(state, sort_keys=True)

        with self.assertRaises(broker.BrokerUnavailableError) as ctx:
            supervision.open_service_session(
                self.repo, self.cfg, store_path=self.db_path, job_id=foreign_job
            )
        self.assertIn("not the active supervised job", str(ctx.exception))
        # Nothing was installed or served: the writer is still absent, neither
        # job recorded a receipt, and the JSON projection is unchanged.
        self.assertIsNone(broker.projection_writer())
        self.assertEqual(self.ledger.receipt_high_water(foreign_job), 0)
        self.assertEqual(self.ledger.receipt_high_water(own_job), 0)
        after = json.dumps(
            state_mod.load_state(self.repo, self.cfg["name"]), sort_keys=True
        )
        self.assertEqual(before, after)

    def test_service_session_accepts_matching_explicit_job_id(self) -> None:
        """An explicit job id equal to the worktree's active job is accepted."""
        self._clear_writer()
        own_job = self.register()
        with supervision.open_service_session(
            self.repo, self.cfg, store_path=self.db_path, job_id=own_job
        ) as session:
            self.assertEqual(session.job_id, own_job)
            self.assertIsNotNone(broker.projection_writer())
        self.assertIsNone(broker.projection_writer())

    def test_service_session_rejects_terminal_explicit_job_id(self) -> None:
        """A terminal job id cannot be selected even if it exists."""
        self._clear_writer()
        own_job = self.register()
        self.ledger.set_job_state(own_job, "completed")
        with self.assertRaises(broker.BrokerUnavailableError):
            supervision.open_service_session(
                self.repo, self.cfg, store_path=self.db_path, job_id=own_job
            )
        self.assertIsNone(broker.projection_writer())

    def test_service_session_corrupt_store_fails_closed(self) -> None:
        """An existing but corrupt store fails closed as BrokerUnavailableError.

        Regression for the review finding that ``open_service_session`` let
        ``ledger_mod.open_ledger`` raise a raw ``sqlite3.DatabaseError`` for an
        unopenable existing store: every unavailable service-store provisioning
        failure must surface as the named broker error *before* a session or
        writer is installed, so a corrupt store can neither leak a raw database
        error nor record or project anything.
        """
        self._clear_writer()
        job_id = self.register()
        corrupt = self.storage / "corrupt.sqlite3"
        # A non-sqlite file at the configured store path is an *existing*
        # but unopenable store (``open_ledger`` fails while reading the
        # recorded schema version), distinct from a missing store.
        corrupt.write_bytes(b"this is not a sqlite database at all\n")
        state = state_mod.load_state(self.repo, self.cfg["name"])
        before = json.dumps(state, sort_keys=True)

        with self.assertRaises(broker.BrokerUnavailableError) as ctx:
            supervision.open_service_session(
                self.repo, self.cfg, store_path=corrupt
            )
        self.assertIn("could not be opened", str(ctx.exception))
        # No session or writer was installed, no receipt was recorded, and the
        # JSON projection is untouched.
        self.assertIsNone(broker.projection_writer())
        self.assertEqual(self.ledger.receipt_high_water(job_id), 0)
        after = json.dumps(
            state_mod.load_state(self.repo, self.cfg["name"]), sort_keys=True
        )
        self.assertEqual(before, after)

    def test_service_session_unsupported_schema_fails_closed(self) -> None:
        """An existing store with an unsupported schema fails closed too.

        The raw ``ledger.LedgerVersionError`` is a ``LedgerError``, not a
        broker error; the bootstrap must translate it like any other
        unavailable-store failure and leave no writer or projection behind.
        """
        import sqlite3

        self._clear_writer()
        job_id = self.register()
        newer = self.storage / "newer.sqlite3"
        conn = sqlite3.connect(str(newer))
        try:
            conn.execute("PRAGMA user_version = 999")
            conn.commit()
        finally:
            conn.close()
        state = state_mod.load_state(self.repo, self.cfg["name"])
        before = json.dumps(state, sort_keys=True)

        with self.assertRaises(broker.BrokerUnavailableError) as ctx:
            supervision.open_service_session(
                self.repo, self.cfg, store_path=newer
            )
        self.assertIn("could not be opened", str(ctx.exception))
        self.assertIsNone(broker.projection_writer())
        self.assertEqual(self.ledger.receipt_high_water(job_id), 0)
        after = json.dumps(
            state_mod.load_state(self.repo, self.cfg["name"]), sort_keys=True
        )
        self.assertEqual(before, after)

    def test_service_host_unbindable_socket_fails_closed(self) -> None:
        """An unbindable endpoint socket fails closed as BrokerUnavailableError.

        Regression for the review finding that ``bind`` leaked a raw ``OSError``
        out of the CLI: the endpoint host must translate socket provisioning
        failures to the named broker error, tear the session (and its installed
        writer) down, and serve nothing.
        """
        from types import SimpleNamespace

        self._clear_writer()
        job_id = self.register()
        # A plain file where the operator socket's parent directory must be:
        # creating the socket directory cannot succeed, so binding is
        # impossible.
        blocker = self.root / "blocker"
        blocker.write_text("not a directory\n", encoding="utf-8")
        socket_env = mock.patch.dict(
            os.environ,
            {
                supervision.OPERATOR_SOCKET_ENV: str(blocker / "operator.sock"),
                supervision.WORKER_SOCKET_ENV: str(self.root / "worker.sock"),
            },
        )
        socket_env.start()
        self.addCleanup(socket_env.stop)
        host = supervision.open_service_host(
            self.repo, self.cfg,
            store_path=self.db_path,
            operator_principal=SimpleNamespace(uid=os.getuid()),
            service_principal=SimpleNamespace(uid=os.getuid()),
        )
        try:
            self.assertIsNotNone(broker.projection_writer())
            with self.assertRaises(broker.BrokerUnavailableError) as ctx:
                host.bind()
            self.assertIn("could not be prepared", str(ctx.exception))
        finally:
            host.close()
        # The failed boot cleaned up: the writer is restored and nothing was
        # bound or served, so no receipt was recorded.
        self.assertIsNone(broker.projection_writer())
        self.assertEqual(self.ledger.receipt_high_water(job_id), 0)
        self.assertFalse(host.operator_socket.exists())

    def test_service_host_worker_socket_failure_cleans_up_operator_socket(self) -> None:
        """A worker-socket failure after the operator bound tears both down."""
        from types import SimpleNamespace

        self._clear_writer()
        job_id = self.register()
        blocker = self.root / "blocker"
        blocker.write_text("not a directory\n", encoding="utf-8")
        operator_sock = self.root / "operator-ok.sock"
        socket_env = mock.patch.dict(
            os.environ,
            {
                supervision.OPERATOR_SOCKET_ENV: str(operator_sock),
                supervision.WORKER_SOCKET_ENV: str(blocker / "worker.sock"),
            },
        )
        socket_env.start()
        self.addCleanup(socket_env.stop)
        host = supervision.open_service_host(
            self.repo, self.cfg,
            store_path=self.db_path,
            operator_principal=SimpleNamespace(uid=os.getuid()),
            service_principal=SimpleNamespace(uid=os.getuid()),
        )
        try:
            with self.assertRaises(broker.BrokerUnavailableError):
                host.bind()
        finally:
            host.close()
        # The operator socket that did bind was cleaned up along with the
        # session, and nothing was served or recorded.
        self.assertFalse(operator_sock.exists())
        self.assertIsNone(broker.projection_writer())
        self.assertEqual(self.ledger.receipt_high_water(job_id), 0)

    def test_supervise_serve_cli_unbindable_socket_fails_closed(self) -> None:
        """``supervise serve`` reports BrokerUnavailableError for a bad socket."""
        from lib.orchestrator import cmd_supervise

        self._clear_writer()
        job_id = self.register()
        plan = self._write_plan()
        blocker = self.root / "blocker"
        blocker.write_text("not a directory\n", encoding="utf-8")
        socket_env = mock.patch.dict(
            os.environ,
            {
                supervision.OPERATOR_SOCKET_ENV: str(blocker / "operator.sock"),
                supervision.WORKER_SOCKET_ENV: str(self.root / "worker.sock"),
            },
        )
        socket_env.start()
        self.addCleanup(socket_env.stop)
        allowed_patch = mock.patch.object(
            supervision, "_allowed_uids_for", return_value=frozenset({os.getuid()})
        )
        allowed_patch.start()
        self.addCleanup(allowed_patch.stop)
        operator_patch = mock.patch.object(
            supervision, "_operator_allowed_uids",
            return_value=frozenset({os.getuid()}),
        )
        operator_patch.start()
        self.addCleanup(operator_patch.stop)
        args = argparse.Namespace(
            repo=str(self.repo),
            plan=str(plan.relative_to(self.repo)),
            store=str(self.db_path),
            job_id=None,
            once=True,
            timeout=1.0,
        )
        with redirect_stderr(io.StringIO()) as err:
            code = cmd_supervise.cmd_supervise_serve(args)
        self.assertEqual(code, 1)
        self.assertIn("BrokerUnavailableError", err.getvalue())
        # No request was served and the writer the session installed was
        # restored by the fail-closed path.
        self.assertIsNone(broker.projection_writer())
        self.assertEqual(self.ledger.receipt_high_water(job_id), 0)

    def test_service_host_binds_and_projects_operator_release(self) -> None:
        """The production host binds sockets and projects a live endpoint receipt."""
        from types import SimpleNamespace

        job_id = self.register()
        socket_env = mock.patch.dict(
            os.environ,
            {
                supervision.OPERATOR_SOCKET_ENV: str(self.root / "operator.sock"),
                supervision.WORKER_SOCKET_ENV: str(self.root / "worker.sock"),
            },
        )
        socket_env.start()
        self.addCleanup(socket_env.stop)
        host = supervision.open_service_host(
            self.repo, self.cfg,
            store_path=self.db_path,
            operator_principal=SimpleNamespace(uid=os.getuid()),
            service_principal=SimpleNamespace(uid=os.getuid()),
        )
        self.addCleanup(host.close)
        self.assertIsNotNone(broker.projection_writer())
        host.bind()
        self.assertTrue(host.operator_socket.exists())

        import threading

        outcome: dict = {}

        def client_call() -> None:
            try:
                outcome["result"] = broker_client.call(
                    {"verb": "approve", "change_ids": ["gated-human"]},
                    kind=endpoints.ENDPOINT_OPERATOR,
                    socket_path=host.operator_socket,
                )
            except broker.BrokerError as exc:
                outcome["error"] = type(exc).__name__

        thread = threading.Thread(target=client_call)
        thread.start()
        result = host.poll(timeout=10)
        thread.join(timeout=10)
        self.assertFalse(thread.is_alive())
        self.assertNotIn("error", outcome, outcome.get("error"))
        self.assertTrue(result["ok"], result)
        self.assertEqual(self.ledger.receipt_high_water(job_id), 1)
        self.assertEqual(
            state_mod.load_state(self.repo, self.cfg["name"])["approvals"],
            ["gated-human"],
        )

    def test_supervise_serve_cli_boots_writer_and_projects_receipt(self) -> None:
        """The ``supervise serve`` command itself installs the writer and serves.

        End-to-end through the CLI handler: the command boots the production
        bootstrap (installing the projection writer), binds the endpoint
        sockets, and dispatches a live operator approval that records a durable
        receipt and regenerates the JSON projection.
        """
        import threading
        import time

        from lib.orchestrator import cmd_supervise

        job_id = self.register()
        plan = self._write_plan()
        operator_sock = self.root / "serve-operator.sock"
        worker_sock = self.root / "serve-worker.sock"
        socket_env = mock.patch.dict(
            os.environ,
            {
                supervision.OPERATOR_SOCKET_ENV: str(operator_sock),
                supervision.WORKER_SOCKET_ENV: str(worker_sock),
            },
        )
        socket_env.start()
        self.addCleanup(socket_env.stop)
        # The test host provisions no distinct principals, so resolve the
        # endpoint accept set to this process's uid. Production resolves the
        # real operator/service principals and fails closed otherwise.
        principal_patch = mock.patch.object(
            supervision, "_operator_allowed_uids", return_value=frozenset({os.getuid()})
        )
        principal_patch.start()
        self.addCleanup(principal_patch.stop)
        allowed_patch = mock.patch.object(
            supervision, "_allowed_uids_for", return_value=frozenset({os.getuid()})
        )
        allowed_patch.start()
        self.addCleanup(allowed_patch.stop)

        args = argparse.Namespace(
            repo=str(self.repo),
            plan=str(plan.relative_to(self.repo)),
            store=str(self.db_path),
            job_id=None,
            once=True,
            timeout=15.0,
        )
        serve_result: dict = {}

        def run_serve() -> None:
            serve_result["rc"] = cmd_supervise.cmd_supervise_serve(args)

        thread = threading.Thread(target=run_serve)
        thread.start()
        try:
            deadline = time.time() + 10
            while not operator_sock.exists() and time.time() < deadline:
                time.sleep(0.05)
            self.assertTrue(operator_sock.exists(), "serve did not bind the socket")
            self.assertEqual(
                broker_client.call(
                    {"verb": "approve", "change_ids": ["gated-human"]},
                    kind=endpoints.ENDPOINT_OPERATOR,
                    socket_path=operator_sock,
                )["approved"],
                ["gated-human"],
            )
        finally:
            thread.join(timeout=15)
        self.assertFalse(thread.is_alive(), "serve did not exit after --once")
        self.assertEqual(serve_result.get("rc"), 0, serve_result)
        self.assertEqual(self.ledger.receipt_high_water(job_id), 1)
        self.assertEqual(
            state_mod.load_state(self.repo, self.cfg["name"])["approvals"],
            ["gated-human"],
        )

    def test_supervise_serve_cli_fails_closed_without_store(self) -> None:
        """A missing service store is refused before any socket is bound."""
        from lib.orchestrator import cmd_supervise

        self._clear_writer()
        self.register()
        plan = self._write_plan()
        args = argparse.Namespace(
            repo=str(self.repo),
            plan=str(plan.relative_to(self.repo)),
            store=str(self.root / "missing" / "supervisor.sqlite3"),
            job_id=None,
            once=True,
            timeout=1.0,
        )
        with redirect_stderr(io.StringIO()) as err:
            code = cmd_supervise.cmd_supervise_serve(args)
        self.assertEqual(code, 1)
        self.assertIn("BrokerUnavailableError", err.getvalue())
        self.assertIsNone(broker.projection_writer())

    def test_supervise_serve_cli_corrupt_store_fails_closed(self) -> None:
        """A corrupt existing service store fails closed through the CLI too.

        Regression for the review finding that an unopenable existing store
        leaked a raw database error: ``supervise serve`` must report the named
        ``BrokerUnavailableError`` and return 1 without installing a writer.
        """
        from lib.orchestrator import cmd_supervise

        self._clear_writer()
        job_id = self.register()
        plan = self._write_plan()
        corrupt = self.storage / "corrupt.sqlite3"
        corrupt.write_bytes(b"this is not a sqlite database at all\n")
        args = argparse.Namespace(
            repo=str(self.repo),
            plan=str(plan.relative_to(self.repo)),
            store=str(corrupt),
            job_id=None,
            once=True,
            timeout=1.0,
        )
        with redirect_stderr(io.StringIO()) as err:
            code = cmd_supervise.cmd_supervise_serve(args)
        self.assertEqual(code, 1)
        self.assertIn("BrokerUnavailableError", err.getvalue())
        self.assertIsNone(broker.projection_writer())
        self.assertEqual(self.ledger.receipt_high_water(job_id), 0)


# ---------------------------------------------------------------------------
# 7.2: material revision binding
# ---------------------------------------------------------------------------


class MaterialRevisionTests(BrokerTestCase):
    def test_stale_material_revision_does_not_satisfy_the_gate(self) -> None:
        job_id = self.register()
        broker.record_approval(
            self.ledger, job_id, principal=self.operator(),
            change_ids=["gated-human"],
        )
        self.assertTrue(broker.is_dispatchable(self.ledger, job_id, "gated-human"))

        # An explicit operator policy revision re-arms the gate: the material
        # revision the receipt bound to is superseded.
        current = self.ledger.current_policy(job_id)
        self.ledger.revise_policy(
            job_id, revision=2,
            policy={
                "authority_config": current["authority_config"],
                "model_selection": current["model_selection"],
                "inexpensive_allowlist": current["inexpensive_allowlist"],
                "manifest_snapshot_hash": current["manifest_snapshot_hash"],
                "budgets": current["budgets"],
                "deadlines": current["deadlines"],
            },
            operator="operator",
        )
        self.assertFalse(broker.is_dispatchable(self.ledger, job_id, "gated-human"))
        resolution = broker.resolve_gate(self.ledger, job_id, "gated-human")
        self.assertFalse(resolution.dispatchable)

    def test_unrelated_updates_do_not_invalidate_the_receipt(self) -> None:
        job_id = self.register()
        broker.record_approval(
            self.ledger, job_id, principal=self.operator(),
            change_ids=["gated-human"],
        )
        # Task progress, telemetry, and another change's state are unrelated to
        # the material gate inputs, so they must not invalidate the receipt.
        self.ledger.begin_action(job_id, kind="implement", run_id="run-1")
        self.ledger.record_incident(job_id, kind="telemetry", summary="noise")
        state = {"changes": {"open-change": {"status": "done"}}}
        (self.repo / ".opsx-plan").mkdir(exist_ok=True)
        (self.repo / ".opsx-plan" / "broker-test.state.json").write_text(
            json.dumps(state), encoding="utf-8"
        )
        self.assertTrue(broker.is_dispatchable(self.ledger, job_id, "gated-human"))

    def test_worker_plan_edits_cannot_shift_the_material_revision(self) -> None:
        job_id = self.register()
        broker.record_approval(
            self.ledger, job_id, principal=self.operator(),
            change_ids=["gated-human"],
        )
        before = broker.material_state(self.ledger, job_id, "gated-human")
        # The worker edits the repo plan to drop the gate entirely.
        plan = self.repo / "openspec" / "plans"
        plan.mkdir(parents=True, exist_ok=True)
        (plan / "broker-test.toml").write_text(
            _manifest('[[changes]]\nid = "gated-human"\n'), encoding="utf-8"
        )
        after = broker.material_state(self.ledger, job_id, "gated-human")
        self.assertEqual(before, after)
        self.assertTrue(broker.is_dispatchable(self.ledger, job_id, "gated-human"))

    def test_resume_with_valid_receipts_passes_and_stale_raises(self) -> None:
        job_id = self.register()
        broker.record_approval(
            self.ledger, job_id, principal=self.operator(),
            change_ids=["gated-human"],
        )
        broker.assert_resume_clear(self.ledger, job_id)
        current = self.ledger.current_policy(job_id)
        self.ledger.revise_policy(
            job_id, revision=2,
            policy={
                "authority_config": current["authority_config"],
                "model_selection": current["model_selection"],
                "inexpensive_allowlist": current["inexpensive_allowlist"],
                "manifest_snapshot_hash": current["manifest_snapshot_hash"],
                "budgets": current["budgets"],
                "deadlines": current["deadlines"],
            },
            operator="operator",
        )
        with self.assertRaises(broker.StaleMaterialError):
            broker.assert_resume_clear(self.ledger, job_id)
        # The stale gate raised a durable incident into the job's incident flow.
        kinds = [row["kind"] for row in self.ledger.list_incidents(job_id)]
        self.assertIn("stale_material", kinds)


# ---------------------------------------------------------------------------
# 7.5 / 7.8: durable wake-up and pause/steer receipts
# ---------------------------------------------------------------------------


class DurableWakeupTests(BrokerTestCase):
    def test_receipt_scan_is_lock_free_and_restart_safe(self) -> None:
        job_id = self.register()
        tracker = broker.ReceiptWakeTracker()
        self.assertEqual(tracker.scan(self.ledger, job_id), [])
        broker.record_approval(
            self.ledger, job_id, principal=self.operator(),
            change_ids=["gated-human"],
        )
        woken = tracker.notify(self.ledger, job_id)
        self.assertEqual([row["change_id"] for row in woken], ["gated-human"])
        self.assertEqual(tracker.scan(self.ledger, job_id), [])
        # A restart constructs a fresh tracker and rescans every receipt above
        # the (zero) high-water mark: nothing is lost.
        restarted = broker.ReceiptWakeTracker()
        rescanned = restarted.scan(self.ledger, job_id)
        self.assertEqual([row["change_id"] for row in rescanned], ["gated-human"])
        # No execution lock was acquired by any receipt path.
        self.assertIsNone(lock.read_record(self.repo))

    def test_high_water_advances_with_receipt_ids(self) -> None:
        job_id = self.register()
        first_receipt = broker.record_approval(
            self.ledger, job_id, principal=self.operator(),
            change_ids=["gated-human"],
        )[0]
        tracker = broker.ReceiptWakeTracker(high_water=first_receipt.receipt_id)
        self.assertEqual(tracker.scan(self.ledger, job_id), [])
        broker.record_pause_or_steer(
            self.ledger, job_id, principal=self.service(),
            change_id="gated-human", kind="pause",
        )
        second = tracker.scan(self.ledger, job_id)
        self.assertEqual([row["kind"] for row in second], ["pause"])

    def test_pause_and_steer_receipts_are_bound_and_wake_the_job(self) -> None:
        job_id = self.register()
        pause = broker.record_pause_or_steer(
            self.ledger, job_id, principal=self.service(),
            change_id="gated-human", kind="pause",
        )
        steer = broker.record_pause_or_steer(
            self.ledger, job_id, principal=self.operator(),
            change_id="gated-human", kind="steer",
        )
        state = broker.material_state(self.ledger, job_id, "gated-human")
        digest = broker.material_hash(
            "gated-human", state.fields, state.snapshot_hash, state.policy_revision
        )
        for receipt in (pause, steer):
            self.assertEqual(receipt.material_hash, digest)
            self.assertEqual(
                receipt.checkpoint, broker.checkpoint_for(receipt.kind, "gated-human")
            )
        tracker = broker.ReceiptWakeTracker()
        woken = tracker.notify(self.ledger, job_id)
        self.assertEqual(
            [row["kind"] for row in woken], ["pause", "steer"]
        )
        self.assertIsNone(lock.read_record(self.repo))

    def test_worker_cannot_record_pause_or_steer(self) -> None:
        job_id = self.register()
        for kind in ("pause", "steer"):
            with self.assertRaises(broker.BrokerMediationError):
                broker.record_pause_or_steer(
                    self.ledger, job_id, principal=self.worker(),
                    change_id="gated-human", kind=kind,
                )
        self.assertEqual(self.ledger.receipt_high_water(job_id), 0)


# ---------------------------------------------------------------------------
# 7.6 / 7.7: CLI mediation, legacy path, and projection
# ---------------------------------------------------------------------------


class CliMediationTests(BrokerTestCase):
    def setUp(self) -> None:
        super().setUp()
        self.opsx_plan = load_opsx_plan()
        self.enable_env()

    def _approve_args(self, *, change=(), approve_all=False) -> argparse.Namespace:
        return argparse.Namespace(
            repo=str(self.repo), plan=None, change=list(change),
            approve_all=approve_all,
        )

    def test_approve_all_is_broker_mediated_and_prints_exact_ids(self) -> None:
        from lib.orchestrator import supervision

        job_id = self.register()
        transport, calls = self._fake_transport(job_id)
        previous = supervision.set_transport(transport)
        self.addCleanup(supervision.set_transport, previous)
        out = io.StringIO()
        with redirect_stdout(out):
            code = self.opsx_plan.cmd_gates._mediated_approve(
                self.repo, self.cfg, self.state, self._approve_args(approve_all=True)
            )
        self.assertEqual(code, 0)
        # Only the human-only gate is awaiting approval.
        self.assertIn("Approved: gated-human", out.getvalue())
        self.assertEqual(calls[0]["request"]["change_ids"], ["gated-human"])
        self.assertEqual(
            self.ledger.receipt_high_water(job_id), 1,
            "approve --all must record exactly the affected receipt",
        )
        # The JSON projection follows broker state.
        projected = self.opsx_plan.state_mod.load_state(self.repo, "broker-test")
        self.assertEqual(projected["approvals"], ["gated-human"])

    def test_approve_phase_is_broker_mediated(self) -> None:
        from lib.orchestrator import supervision

        job_id = self.register()
        transport, calls = self._fake_transport(job_id)
        previous = supervision.set_transport(transport)
        self.addCleanup(supervision.set_transport, previous)
        out = io.StringIO()
        with redirect_stdout(out):
            code = self.opsx_plan.cmd_gates._mediated_approve(
                self.repo, self.cfg, self.state, self._approve_args(change=["P1"])
            )
        self.assertEqual(code, 0)
        self.assertIn("Approved: gated-human", out.getvalue())
        # P1 resolves against the protected snapshot: exactly the human-only
        # gate in phase 1.
        self.assertEqual(
            calls[0]["request"]["change_ids"], ["gated-human"]
        )

    def test_accept_is_broker_mediated(self) -> None:
        from lib.orchestrator import supervision

        job_id = self.register(content=_manifest(
            HUMAN_GATED, DELEGATED_GATED, UNGATED, review_created=True
        ))
        cfg = dict(self.cfg)
        cfg["review_created"] = True
        state = {"plan": "broker-test", "approvals": [], "changes": {
            "gated-human": {"created_by_orchestrator": True}
        }}
        self._write_authored("gated-human")
        transport, calls = self._fake_transport(job_id)
        previous = supervision.set_transport(transport)
        self.addCleanup(supervision.set_transport, previous)
        args = argparse.Namespace(
            repo=str(self.repo), plan=None, change=["gated-human"], accept_all=False
        )
        out = io.StringIO()
        with redirect_stdout(out):
            code = self.opsx_plan.cmd_gates._mediated_accept(
                self.repo, cfg, state, args
            )
        self.assertEqual(code, 0)
        self.assertIn("Accepted: gated-human", out.getvalue())
        self.assertEqual(calls[0]["request"]["verb"], "accept")

    def test_tampered_markers_do_not_disable_mediation(self) -> None:
        from lib.orchestrator import supervision

        job_id = self.register()
        transport, calls = self._fake_transport(job_id)
        previous = supervision.set_transport(transport)
        self.addCleanup(supervision.set_transport, previous)
        # A worker removes every supervised-looking field and rewrites the repo
        # plan; registration comes from the service-owned ledger, so the job is
        # still mediated.
        (self.repo / ".opsx-plan").mkdir(exist_ok=True)
        (self.repo / ".opsx-plan" / "broker-test.state.json").write_text(
            json.dumps({"plan": "broker-test", "changes": {}}), encoding="utf-8"
        )
        plans = self.repo / "openspec" / "plans"
        plans.mkdir(parents=True, exist_ok=True)
        (plans / "broker-test.toml").write_text(
            _manifest('[[changes]]\nid = "gated-human"\n'), encoding="utf-8"
        )
        with redirect_stdout(io.StringIO()):
            code = self.opsx_plan.cmd_gates._mediated_approve(
                self.repo, self.cfg, self.state, self._approve_args(approve_all=True)
            )
        self.assertEqual(code, 0)
        self.assertTrue(calls, "the broker path must still be taken")

    def test_unreachable_broker_fails_closed(self) -> None:
        job_id = self.register()
        plan = self._write_plan()
        # No transport installed and no socket configured: fail closed.
        self.enable_env()
        args = argparse.Namespace(
            repo=str(self.repo), plan=str(plan.relative_to(self.repo)),
            change=["gated-human"], approve_all=False,
        )
        with redirect_stderr(io.StringIO()) as err:
            code = self.opsx_plan.cmd_gates.cmd_approve(args)
        self.assertEqual(code, 2)
        self.assertIn("BrokerUnavailableError", err.getvalue())
        self.assertEqual(self.ledger.receipt_high_water(job_id), 0)

    def test_worker_domain_post_to_operator_endpoint_is_refused(self) -> None:
        """A worker principal cannot authenticate against the operator endpoint."""
        job_id = self.register()
        server, client = socket.socketpair(socket.AF_UNIX, socket.SOCK_STREAM)
        self.addCleanup(server.close)
        self.addCleanup(client.close)
        real = endpoints.peer_credentials(server)
        worker = endpoints.PeerCredentials(
            pid=real.pid, uid=real.uid + 1, gid=real.gid
        )
        endpoint = endpoints.Endpoint(
            endpoints.ENDPOINT_OPERATOR, frozenset({real.uid})
        )

        def reader(conn):
            payload = broker_client.read_json_request(conn)
            enriched = dict(payload)
            enriched["ledger"] = self.ledger
            enriched["job_id"] = job_id
            return enriched

        with mock.patch.object(endpoints, "peer_credentials", return_value=worker):
            with self.assertRaises(endpoints.PeerCredentialError):
                endpoint.handle(server, read_request=reader)
        self.assertEqual(self.ledger.receipt_high_water(job_id), 0)

    def test_projection_follows_broker_state_over_direct_edits(self) -> None:
        from lib.orchestrator import supervision

        job_id = self.register()
        transport, _calls = self._fake_transport(job_id)
        previous = supervision.set_transport(transport)
        self.addCleanup(supervision.set_transport, previous)
        with redirect_stdout(io.StringIO()):
            self.opsx_plan.cmd_gates._mediated_approve(
                self.repo, self.cfg, self.state, self._approve_args(approve_all=True)
            )
        # A direct JSON edit removing the approval has no authority: the broker
        # receipt still satisfies the gate.
        state = self.opsx_plan.state_mod.load_state(self.repo, "broker-test")
        state["approvals"] = []
        self.opsx_plan.state_mod.save_state(self.repo, "broker-test", state)
        self.assertTrue(broker.is_dispatchable(self.ledger, job_id, "gated-human"))
        # Regenerating the projection restores the broker-derived approval.
        supervision.persist_projection(
            self.repo, self.cfg,
            self.opsx_plan.state_mod.load_state(self.repo, "broker-test"),
            self.ledger, job_id,
        )
        restored = self.opsx_plan.state_mod.load_state(self.repo, "broker-test")
        self.assertEqual(restored["approvals"], ["gated-human"])

    def test_reset_is_broker_mediated_and_does_not_take_the_lock(self) -> None:
        from lib.orchestrator import supervision

        job_id = self.register()
        transport, calls = self._fake_transport(job_id)
        previous = supervision.set_transport(transport)
        self.addCleanup(supervision.set_transport, previous)
        args = argparse.Namespace(
            repo=str(self.repo), plan=None, change=["gated-human"], failed=False
        )
        with redirect_stdout(io.StringIO()):
            code = self.opsx_plan.cmd_gates._mediated_reset(
                self.repo, self.cfg, args
            )
        self.assertEqual(code, 0)
        self.assertEqual(calls[0]["request"]["verb"], "reset_change")
        self.assertEqual(
            [row["kind"] for row in self.ledger.receipts_for_change(
                job_id, "gated-human")],
            ["reset"],
        )
        # A broker reset never acquires the worktree execution lock.
        self.assertIsNone(lock.read_record(self.repo))

    # -- legacy unregistered jobs (7.6) -----------------------------------

    def test_legacy_unregistered_approve_uses_json_without_backend(self) -> None:
        plan = self._write_plan()
        # Drop the fixture's ledger so the worktree is genuinely unregistered.
        self.ledger.close()
        for path in (
            self.db_path, Path(str(self.db_path) + "-wal"),
            Path(str(self.db_path) + "-shm"),
        ):
            if path.exists():
                path.unlink()
        self.enable_env(ledger=False)
        args = argparse.Namespace(
            repo=str(self.repo), plan=str(plan.relative_to(self.repo)),
            change=["gated-human"], approve_all=False,
        )
        code = self.opsx_plan.cmd_gates.cmd_approve(args)
        self.assertEqual(code, 0)
        state = self.opsx_plan.state_mod.load_state(self.repo, "broker-test")
        self.assertIn("gated-human", state["approvals"])
        self.assertFalse(self.db_path.exists(), "no ledger may be created")

    def test_legacy_unregistered_reset_uses_json_without_backend(self) -> None:
        plan = self._write_plan()
        self.ledger.close()
        for path in (
            self.db_path, Path(str(self.db_path) + "-wal"),
            Path(str(self.db_path) + "-shm"),
        ):
            if path.exists():
                path.unlink()
        self.enable_env(ledger=False)
        state = self.opsx_plan.state_mod.load_state(self.repo, "broker-test")
        self.opsx_plan.state_mod.set_status(
            state, "gated-human", self.opsx_plan.base.FAILED, "boom"
        )
        self.opsx_plan.state_mod.save_state(self.repo, "broker-test", state)
        args = argparse.Namespace(
            repo=str(self.repo), plan=str(plan.relative_to(self.repo)),
            change=["gated-human"], failed=False,
        )
        code = self.opsx_plan.cmd_gates.cmd_reset(args)
        self.assertEqual(code, 0)
        after = self.opsx_plan.state_mod.load_state(self.repo, "broker-test")
        self.assertEqual(
            after["changes"]["gated-human"]["status"], self.opsx_plan.base.PENDING
        )

    def _write_authored(self, cid: str) -> None:
        cdir = self.repo / "openspec" / "changes" / cid
        cdir.mkdir(parents=True, exist_ok=True)
        (cdir / "proposal.md").write_text("## Why\n", encoding="utf-8")
        (cdir / "tasks.md").write_text("- [x] 1.1 task\n", encoding="utf-8")


# ---------------------------------------------------------------------------
# Review regressions: hostile environments and tampered phase resolution
# ---------------------------------------------------------------------------


class HostileEnvironmentRegressionTests(BrokerTestCase):
    """Regressions for the review findings on broker-mediation bypasses.

    A worker controls its environment and the repo-writable plan and fencing
    record; none of them may downgrade a registered job to the unmediated
    legacy path or authorize dispatch.
    """

    def setUp(self) -> None:
        super().setUp()
        self.opsx_plan = load_opsx_plan()
        self.enable_env()
        self.plan_path = self._write_plan()

    def _approve_args(self, *, change=(), approve_all=False) -> argparse.Namespace:
        return argparse.Namespace(
            repo=str(self.repo), plan=str(self.plan_path.relative_to(self.repo)),
            change=list(change), approve_all=approve_all,
        )

    def test_missing_state_file_cannot_hide_registration(self) -> None:
        """A nonexistent external store must not re-enable legacy JSON mutation."""
        from lib.orchestrator import supervision

        job_id = self.register()
        missing = self.root / "external" / "supervisor.sqlite3"
        patcher = mock.patch.dict(
            os.environ, {"OPSX_SUPERVISOR_STATE_FILE": str(missing)}, clear=True
        )
        patcher.start()
        self.addCleanup(patcher.stop)

        with redirect_stderr(io.StringIO()) as err:
            code = self.opsx_plan.cmd_gates.cmd_approve(self._approve_args(
                change=["gated-human"]
            ))
        self.assertEqual(code, 2, err.getvalue())
        self.assertIn("BrokerUnavailableError", err.getvalue())
        # Legacy JSON was not mutated and no receipt was recorded.
        state = self.opsx_plan.state_mod.load_state(self.repo, "broker-test")
        self.assertNotIn("gated-human", state["approvals"])
        self.assertEqual(self.ledger.receipt_high_water(job_id), 0)

    def test_repointed_state_file_cannot_hide_registration(self) -> None:
        """A configured store that holds no job must not hide the real one.

        The service-owned store holding the registered job is always consulted,
        so repointing the override at an empty-but-valid ledger neither
        downgrades mediation nor mutates JSON.
        """
        from lib.orchestrator import supervision

        job_id = self.register()
        other = self.root / "other-storage"
        other.mkdir()
        other_path = other / "supervisor.sqlite3"
        other_handle = ledger.open_ledger(other_path, repository_root=self.repo)
        other_handle.close()
        patcher = mock.patch.dict(
            os.environ, {"OPSX_SUPERVISOR_STATE_FILE": str(other_path)}, clear=True
        )
        patcher.start()
        self.addCleanup(patcher.stop)
        # Make the service-owned candidate the registered fixture store.
        patcher2 = mock.patch.object(
            supervision, "_authority_service_store", return_value=self.db_path
        )
        patcher2.start()
        self.addCleanup(patcher2.stop)

        transport, calls = self._fake_transport(job_id)
        previous = supervision.set_transport(transport)
        self.addCleanup(supervision.set_transport, previous)
        with redirect_stdout(io.StringIO()):
            code = self.opsx_plan.cmd_gates._mediated_approve(
                self.repo, self.cfg, self.state, self._approve_args(
                    change=["gated-human"]
                )
            )
        self.assertEqual(code, 0)
        self.assertTrue(calls, "the registered job must still be broker mediated")

    def test_execution_marker_env_cannot_enable_dispatch(self) -> None:
        """OPSX_SUPERVISED_EXECUTION=1 is not dispatch authorization."""
        job_id = self.register()
        patcher = mock.patch.dict(
            os.environ, {"OPSX_SUPERVISED_EXECUTION": "1"}
        )
        patcher.start()
        self.addCleanup(patcher.stop)
        spawned: list[str] = []
        with mock.patch.object(
            self.opsx_plan, "run_direct_change",
            side_effect=lambda *a, **k: spawned.append("called"),
        ):
            with redirect_stderr(io.StringIO()) as err:
                code = self.opsx_plan.cmd_run_one.cmd_run_one(
                    argparse.Namespace(repo=str(self.repo), change="gated-human")
                )
        self.assertEqual(code, 2, err.getvalue())
        self.assertIn("BrokerMediationError", err.getvalue())
        self.assertEqual(spawned, [], "no dispatch may occur")
        self.assertEqual(self.ledger.receipt_high_water(job_id), 0)

    def test_repo_writable_fence_cannot_authorize_dispatch(self) -> None:
        """A forged .opsx-plan fencing record is not service-owned evidence."""
        import socket as socket_mod
        from lib.supervisor import lock

        job_id = self.register()
        opsx = self.repo / ".opsx-plan"
        opsx.mkdir(parents=True, exist_ok=True)
        forged = {
            "version": 1, "state": "held", "owner": "forged",
            "owner_kind": "supervised", "job_id": job_id,
            "pid": os.getpid(), "process_start": 0.0,
            "boot_id": lock.boot_identity(), "host": socket_mod.gethostname(),
            "acquired_at": "2026-01-01T00:00:00+00:00", "released_at": None,
        }
        (opsx / "execution-lock.json").write_text(
            json.dumps(forged), encoding="utf-8"
        )
        spawned: list[str] = []
        with mock.patch.object(
            self.opsx_plan, "run_direct_change",
            side_effect=lambda *a, **k: spawned.append("called"),
        ):
            with redirect_stderr(io.StringIO()) as err:
                code = self.opsx_plan.cmd_run_one.cmd_run_one(
                    argparse.Namespace(repo=str(self.repo), change="gated-human")
                )
        self.assertEqual(code, 2, err.getvalue())
        self.assertIn("BrokerMediationError", err.getvalue())
        self.assertEqual(spawned, [])

    def test_assigned_marker_attributes_remain_inert(self) -> None:
        """Setting the module's internal attributes cannot authorize dispatch.

        Regression for the review finding: a worker could import
        ``supervised_execution()`` (or assign a supervised-looking module
        attribute) before calling the dispatch APIs and skip the live
        service-owned fence plus ancestry check. The process-local marker was
        removed, so these assignments must have no effect and an ordinary
        dispatch must still be refused.
        """
        from lib.orchestrator import supervision

        job_id = self.register()
        # A worker-style forgery: assign every marker-shaped module attribute
        # the old implementation exposed, plus an env marker.
        for name in ("_ACTIVE_SUPERVISED", "_SUPERVISED", "_IN_SUPERVISED_EXECUTION"):
            if hasattr(supervision, name):
                setattr(supervision, name, True)
        patcher = mock.patch.dict(
            os.environ,
            {
                "OPSX_SUPERVISED_EXECUTION": "1",
                "OPSX_IN_SUPERVISED_EXECUTION": "1",
            },
        )
        patcher.start()
        self.addCleanup(patcher.stop)
        spawned: list[str] = []
        with mock.patch.object(
            self.opsx_plan, "run_direct_change",
            side_effect=lambda *a, **k: spawned.append("called"),
        ):
            with redirect_stderr(io.StringIO()) as err:
                code = self.opsx_plan.cmd_run_one.cmd_run_one(
                    argparse.Namespace(repo=str(self.repo), change="gated-human")
                )
        self.assertEqual(code, 2, err.getvalue())
        self.assertIn("BrokerMediationError", err.getvalue())
        self.assertEqual(spawned, [], "no dispatch may occur")
        self.assertEqual(self.ledger.receipt_high_water(job_id), 0)

    def test_in_process_execution_entrypoint_does_not_authorize_dispatch(self) -> None:
        """A worker that imports the module API still needs a live ledger fence.

        ``supervised_execution`` was a public in-process seam a worker could
        enter to bypass the fence. The API was removed entirely; neither the
        old name nor an assigned module marker may authorize a dispatch, and
        the in-process ``in_supervised_execution`` probe must report false.
        """
        from lib.orchestrator import supervision

        job_id = self.register()
        self.assertFalse(hasattr(supervision, "supervised_execution"))
        self.assertFalse(
            supervision.in_supervised_execution(self.repo),
            "no live fence exists, so no process may claim supervised execution",
        )
        # Marking the module directly (the removed implementation's mechanism)
        # must not change the decision.
        supervision._ACTIVE_SUPERVISED = True
        self.assertFalse(supervision.in_supervised_execution(self.repo))
        with self.assertRaises(broker.BrokerMediationError):
            supervision.require_supervised_authorization(self.repo)
        self.assertEqual(self.ledger.receipt_high_water(job_id), 0)

    def test_service_owned_live_fence_authorizes_dispatch(self) -> None:
        """A live service-owned fence whose identity is this process authorizes."""
        from lib.supervisor import lock
        from lib.orchestrator import supervision

        job_id = self.register()
        pid = os.getpid()
        self.ledger.record_fencing(
            job_id, event="acquired", owner="svc",
            pid=pid, process_start=lock.process_start_time(pid),
            boot_id=lock.boot_identity(), host="host",
        )
        registration = supervision.require_supervised_authorization(self.repo)
        self.assertIsNotNone(registration)
        registration.close()

    def test_released_ledger_fence_does_not_authorize_dispatch(self) -> None:
        """A released service-owned fence is no longer dispatch authorization."""
        job_id = self.register()
        # Record a supervised fence and immediately release it (a finished
        # supervised execution). Its process identity is this live process, so
        # only the released event distinguishes it from a running execution.
        fence_pid = os.getpid()
        record = {
            "pid": fence_pid,
            "process_start": __import__("lib.supervisor.lock", fromlist=["x"]).process_start_time(fence_pid),
            "boot_id": __import__("lib.supervisor.lock", fromlist=["x"]).boot_identity(),
        }
        self.ledger.record_fencing(
            job_id, event="acquired", owner="svc",
            pid=record["pid"], process_start=record["process_start"],
            boot_id=record["boot_id"], host="host",
        )
        self.ledger.record_fencing(
            job_id, event="released", owner="svc",
            pid=record["pid"], process_start=record["process_start"],
            boot_id=record["boot_id"], host="host",
        )
        spawned: list[str] = []
        with mock.patch.object(
            self.opsx_plan, "run_direct_change",
            side_effect=lambda *a, **k: spawned.append("called"),
        ):
            with redirect_stderr(io.StringIO()) as err:
                code = self.opsx_plan.cmd_run_one.cmd_run_one(
                    argparse.Namespace(repo=str(self.repo), change="gated-human")
                )
        self.assertEqual(code, 2, err.getvalue())
        self.assertIn("BrokerMediationError", err.getvalue())
        self.assertEqual(spawned, [])

    def test_tampered_repo_phase_cannot_redirect_phase_approval(self) -> None:
        """P<N> resolves from the protected snapshot, not the repo plan."""
        from lib.orchestrator import supervision

        job_id = self.register()
        transport, calls = self._fake_transport(job_id)
        previous = supervision.set_transport(transport)
        self.addCleanup(supervision.set_transport, previous)
        # The worker rewrites the repo plan (and the in-memory cfg it can
        # influence) to move gated-human out of phase 1 and gated-delegated in.
        (self.repo / "openspec" / "plans" / "broker-test.toml").write_text(
            _manifest(
                "[[changes]]\nid = \"gated-human\"\nphase = 9\npause_before = true\n",
                "[[changes]]\nid = \"gated-delegated\"\nphase = 1\n"
                "pause_before = true\npause_before_human_only = false\n",
            ),
            encoding="utf-8",
        )
        tampered_cfg = dict(self.cfg)
        tampered_cfg["changes"] = dict(self.cfg["changes"])
        tampered_cfg["changes"]["gated-human"] = dict(
            self.cfg["changes"]["gated-human"], phase=9
        )
        tampered_cfg["changes"]["gated-delegated"] = dict(
            self.cfg["changes"]["gated-delegated"], phase=1
        )
        out = io.StringIO()
        with redirect_stdout(out):
            code = self.opsx_plan.cmd_gates._mediated_approve(
                self.repo, tampered_cfg, self.state, self._approve_args(change=["P1"])
            )
        self.assertEqual(code, 0, out.getvalue())
        # P1 still resolves the protected snapshot's phase-1 human-only gate.
        self.assertEqual(calls[0]["request"]["change_ids"], ["gated-human"])

    def test_tampered_repo_order_cannot_redirect_approve_all(self) -> None:
        """approve --all derives membership and order from the snapshot."""
        from lib.orchestrator import supervision

        job_id = self.register()
        transport, calls = self._fake_transport(job_id)
        previous = supervision.set_transport(transport)
        self.addCleanup(supervision.set_transport, previous)
        tampered_cfg = dict(self.cfg)
        tampered_cfg["order"] = ["gated-delegated"]
        tampered_cfg["changes"] = {"gated-delegated": self.cfg["changes"]["gated-delegated"]}
        with redirect_stdout(io.StringIO()):
            code = self.opsx_plan.cmd_gates._mediated_approve(
                self.repo, tampered_cfg, self.state, self._approve_args(approve_all=True)
            )
        self.assertEqual(code, 0)
        # Only the snapshot's human-only phase-1 gate is affected; the repo
        # cfg's order (delegated only) had no authority.
        self.assertEqual(calls[0]["request"]["change_ids"], ["gated-human"])


# ---------------------------------------------------------------------------
# 5.2 / 7.1: flag semantics and the worker-domain refusal path
# ---------------------------------------------------------------------------


class FlagSemanticsTests(BrokerTestCase):
    """The three ``pause_before_human_only`` cases end to end under supervision."""

    def test_absent_key_gate_releases_only_via_operator_receipt(self) -> None:
        job_id = self.register(content=_manifest(
            '[[changes]]\nid = "gated-human"\npause_before = true\n'
        ))
        self.assertFalse(broker.is_dispatchable(self.ledger, job_id, "gated-human"))
        # A scoped service release is refused: the absent key resolves human-only.
        with self.assertRaises(broker.BrokerMediationError):
            broker.release_delegated_gate(
                self.ledger, job_id, principal=self.service(),
                change_id="gated-human",
            )
        broker.record_approval(
            self.ledger, job_id, principal=self.operator(),
            change_ids=["gated-human"],
        )
        self.assertTrue(broker.is_dispatchable(self.ledger, job_id, "gated-human"))

    def test_explicit_true_gate_releases_only_via_operator_receipt(self) -> None:
        job_id = self.register(content=_manifest(
            '[[changes]]\nid = "gated-human"\npause_before = true\n'
            "pause_before_human_only = true\n"
        ))
        state = broker.material_state(self.ledger, job_id, "gated-human")
        self.assertEqual(state.authority, broker.HUMAN_ONLY)
        with self.assertRaises(broker.BrokerMediationError):
            broker.release_delegated_gate(
                self.ledger, job_id, principal=self.service(),
                change_id="gated-human",
            )
        broker.record_approval(
            self.ledger, job_id, principal=self.operator(),
            change_ids=["gated-human"],
        )
        self.assertTrue(broker.is_dispatchable(self.ledger, job_id, "gated-human"))

    def test_explicit_false_gate_releases_via_scoped_delegated_action(self) -> None:
        job_id = self.register(content=_manifest(
            '[[changes]]\nid = "gated-delegated"\npause_before = true\n'
            "pause_before_human_only = false\n"
        ))
        state = broker.material_state(self.ledger, job_id, "gated-delegated")
        self.assertEqual(state.authority, broker.DELEGATED)
        # Operator approval of a delegated gate is refused by construction.
        with self.assertRaises(broker.BrokerMediationError):
            broker.record_approval(
                self.ledger, job_id, principal=self.operator(),
                change_ids=["gated-delegated"],
            )
        broker.release_delegated_gate(
            self.ledger, job_id, principal=self.service(),
            change_id="gated-delegated",
        )
        self.assertTrue(
            broker.is_dispatchable(self.ledger, job_id, "gated-delegated")
        )


class WorkerDomainSubprocessRefusalTests(BrokerTestCase):
    """7.1: a worker-domain subprocess cannot approve, reset, or run a job.

    The authority fixtures' real restricted-process path is exercised by
    :class:`RealRestrictedProcessBrokerTest` when a provisioned worker principal
    exists. These tests exercise the same refusal structurally through a real
    subprocess with no operator path available, which is the state every
    unauthenticated/worker-domain caller is in.
    """

    def setUp(self) -> None:
        super().setUp()
        self.plan_path = self._write_plan()

    def _run_cli(self, *argv: str):
        env = dict(os.environ)
        env["PYTHONPATH"] = str(REPO_ROOT)
        env["OPSX_SUPERVISOR_STATE_FILE"] = str(self.db_path)
        env.pop(broker_client.OPERATOR_SOCKET_ENV, None)
        env.pop(broker_client.WORKER_SOCKET_ENV, None)
        for key, value in _STAGE_ENV.items():
            env.setdefault(key, value)
        return subprocess.run(
            [sys.executable, str(SCRIPT), *argv],
            cwd=str(self.repo), env=env, capture_output=True, text=True,
        )

    def test_worker_subprocess_cannot_approve(self) -> None:
        job_id = self.register()
        proc = self._run_cli(
            "--repo", str(self.repo), "approve",
            str(self.plan_path.relative_to(self.repo)), "gated-human",
        )
        self.assertEqual(proc.returncode, 2, proc.stdout + proc.stderr)
        self.assertIn("BrokerUnavailableError", proc.stderr)
        self.assertEqual(self.ledger.receipt_high_water(job_id), 0)

    def test_worker_subprocess_cannot_reset(self) -> None:
        job_id = self.register()
        proc = self._run_cli(
            "--repo", str(self.repo), "reset",
            str(self.plan_path.relative_to(self.repo)), "gated-human",
        )
        self.assertEqual(proc.returncode, 2, proc.stdout + proc.stderr)
        self.assertIn("BrokerUnavailableError", proc.stderr)
        self.assertEqual(self.ledger.receipt_high_water(job_id), 0)

    def test_ordinary_subprocess_cannot_run_a_registered_job(self) -> None:
        job_id = self.register()
        proc = self._run_cli(
            "--repo", str(self.repo), "run",
            str(self.plan_path.relative_to(self.repo)),
            "--no-branch", "--no-pr", "--skip-openspec",
        )
        self.assertEqual(proc.returncode, 2, proc.stdout + proc.stderr)
        self.assertIn("BrokerMediationError", proc.stderr)
        self.assertEqual(self.ledger.receipt_high_water(job_id), 0)

    def test_ordinary_subprocess_cannot_run_one_a_registered_job(self) -> None:
        job_id = self.register()
        proc = self._run_cli(
            "--repo", str(self.repo), "run-one", "gated-human",
        )
        self.assertEqual(proc.returncode, 2, proc.stdout + proc.stderr)
        self.assertIn("BrokerMediationError", proc.stderr)
        self.assertEqual(self.ledger.receipt_high_water(job_id), 0)

    def test_worker_process_with_old_marker_helpers_still_refused(self) -> None:
        """A restricted worker subprocess that forges the marker is still refused.

        The worker imports the supervision module, invokes the removed
        ``supervised_execution()`` entrypoint when present, sets module markers,
        and then drives ``run`` and ``run-one``. None of it may authorize a
        dispatch; both commands must refuse with ``BrokerMediationError``.
        """
        job_id = self.register()
        env = dict(os.environ)
        env["PYTHONPATH"] = str(REPO_ROOT)
        env["OPSX_SUPERVISOR_STATE_FILE"] = str(self.db_path)
        env.pop(broker_client.OPERATOR_SOCKET_ENV, None)
        env.pop(broker_client.WORKER_SOCKET_ENV, None)
        env["OPSX_SUPERVISED_EXECUTION"] = "1"
        for key, value in _STAGE_ENV.items():
            env.setdefault(key, value)
        probe = (
            "import os, sys\n"
            "from lib.orchestrator import supervision\n"
            "if hasattr(supervision, 'supervised_execution'):\n"
            "    ctx = supervision.supervised_execution()\n"
            "    ctx.__enter__()\n"
            "for name in ('_ACTIVE_SUPERVISED', '_SUPERVISED'):\n"
            "    setattr(supervision, name, True)\n"
            "from lib.orchestrator import cmd_run_one\n"
            "from lib.supervisor import broker\n"
            "repo = sys.argv[1]\n"
            "rc = cmd_run_one.cmd_run_one(\n"
            "    __import__('argparse').Namespace(repo=repo, change='gated-human')\n"
            ")\n"
            "print('RUN_ONE_RC=%d' % rc, file=sys.stderr)\n"
        )
        proc = subprocess.run(
            [sys.executable, "-c", probe, str(self.repo)],
            cwd=str(self.repo), env=env, capture_output=True, text=True,
        )
        self.assertIn("RUN_ONE_RC=2", proc.stderr, proc.stdout + proc.stderr)
        self.assertIn("BrokerMediationError", proc.stderr)
        # And `run` is refused the same way in a fresh restricted process.
        run_proc = self._run_cli(
            "--repo", str(self.repo), "run",
            str(self.plan_path.relative_to(self.repo)),
            "--no-branch", "--no-pr", "--skip-openspec",
        )
        self.assertEqual(run_proc.returncode, 2, run_proc.stdout + run_proc.stderr)
        self.assertIn("BrokerMediationError", run_proc.stderr)
        self.assertEqual(self.ledger.receipt_high_water(job_id), 0)


class RealRestrictedProcessBrokerTest(unittest.TestCase):
    """7.1: conditional real worker-principal refusal, skipped when unavailable."""

    @unittest.skipUnless(sys.platform.startswith("linux"), "requires Linux")
    def test_real_worker_domain_cannot_approve_in_a_registered_job(self) -> None:
        worker_name = os.environ.get(authority.WORKER_PRINCIPAL_ENV)
        if not worker_name:
            self.skipTest(
                f"{authority.WORKER_PRINCIPAL_ENV} not set; no distinct provisioned "
                "worker principal is available and this suite provisions nothing"
            )
        worker = authority.resolve_principal("worker", worker_name)
        if not worker.exists:
            self.skipTest(f"worker principal '{worker_name}' does not exist on this host")
        if worker.uid == os.getuid():
            self.skipTest("worker principal shares the current uid; no real boundary")
        mechanism = authority.discover_switch_mechanism(worker_name)
        if not mechanism:
            self.skipTest("no usable restricted-spawn mechanism is available")

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            repo = root / "repo"
            repo.mkdir()
            git(repo, "init")
            (repo / "tracked.txt").write_text("base\n", encoding="utf-8")
            git(repo, "add", "tracked.txt")
            git(
                repo, "-c", "user.email=t@example.invalid", "-c", "user.name=T",
                "commit", "-m", "init",
            )
            storage = root / "storage"
            storage.mkdir()
            db_path = storage / "supervisor.sqlite3"
            handle = ledger.open_ledger(db_path, repository_root=repo)
            handle.register_job(
                run_id="run-1", worktree=repo, owner="service",
                owner_principal=worker_name, policy=_policy(), operator="operator",
                manifest_content=_manifest(HUMAN_GATED),
            )
            handle.close()
            env = dict(os.environ)
            env["PYTHONPATH"] = str(REPO_ROOT)
            env["OPSX_SUPERVISOR_STATE_FILE"] = str(db_path)
            env["OPWS_SUPERVISOR_STATE_FILE"] = str(db_path)
            try:
                proc = subprocess.run(
                    [*mechanism, sys.executable, str(SCRIPT), "approve",
                     "--repo", str(repo), "gated-human"],
                    cwd=str(repo), env=env, capture_output=True, text=True,
                )
            except OSError as exc:
                self.skipTest(f"restricted spawn is not usable here: {exc}")
            # The worker cannot reach or authenticate the operator endpoint;
            # the command is refused and no receipt is recorded.
            self.assertNotEqual(proc.returncode, 0, proc.stdout)
            reopened = ledger.open_ledger(db_path, repository_root=repo)
            self.addCleanup(reopened.close)
            self.assertEqual(reopened.receipt_high_water(1), 0)


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
