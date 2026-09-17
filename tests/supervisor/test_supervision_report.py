"""Tests for the supervision observability and steering contract.

Covers change ``add-supervision-observability``:

- the read-only projection field model (job progress, actions, incidents,
  evidence, observed usage, waits, budget limits, steering acknowledgements);
- read-only behavior: the ledger is unchanged and no execution lock is taken;
- steering request identity, single safe-boundary acknowledgement, and survival
  across a restart;
- reboot-deduplicated notifications and a notification failure that never loses
  a gate;
- distinct-but-linked supervision identities versus ``run_id``;
- the cost-per-correct-completion metric definition with no performance claim.
"""

from __future__ import annotations

import json
import os
import struct
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from lib.orchestrator import supervision as supervision_mod
from lib.supervisor import broker, broker_client, endpoints, ledger, lock, model_policy


def git(repo: Path, *args: str) -> None:
    subprocess.run(
        ["git", *args], cwd=repo, check=True, capture_output=True, text=True
    )


def _policy() -> dict:
    return {
        "authority_config": {"mode": "policy-bound"},
        "model_selection": {
            "version": model_policy.MODEL_POLICY_VERSION,
            "roles": {"implementer": "cheap/model-a"},
            "stages": {"implement": "implementer"},
        },
        "inexpensive_allowlist": {
            "version": model_policy.MODEL_POLICY_VERSION,
            "models": ["cheap/model-a"],
            "source": "test fixture",
        },
        "manifest_snapshot_hash": "placeholder",
        "budgets": {
            "version": 1,
            "total_cost_usd": 10.0,
            "per_action_cost_usd": None,
            "total_elapsed_minutes": None,
            "per_action_elapsed_minutes": None,
            "max_incident_attempts": None,
        },
        "deadlines": {"version": 1, "execution_deadline_minutes": None},
    }


def _manifest(*entries: str) -> str:
    body = "\n".join(entries)
    return (
        "[plan]\n"
        'name = "supervision-test"\n'
        'adapter = "opencode"\n'
        'created_check = ""\n'
        "review_created = false\n\n"
        f"{body}\n"
    )


HUMAN_GATED = (
    "[[changes]]\n"
    'id = "gated-human"\n'
    "phase = 1\n"
    "pause_before = true\n"
)
UNGATED = "[[changes]]\n" 'id = "open-change"\n' "phase = 2\n"


class SupervisionReportTestCase(unittest.TestCase):
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
        self.ledger = self._open()

        env = dict(os.environ)
        env["OPSX_SUPERVISOR_STATE_FILE"] = str(self.db_path)
        env["HOME"] = str(self.root / "home")
        patcher = mock.patch.dict(os.environ, env, clear=True)
        patcher.start()
        self.addCleanup(patcher.stop)
        # The trusted service installs the projection writer at boot; the
        # direct broker surface requires it, so install a no-op recorder.
        self.addCleanup(broker.set_projection_writer, broker.projection_writer())
        broker.set_projection_writer(lambda conn, job_id: None)

    def _open(self) -> ledger.Ledger:
        handle = ledger.open_ledger(self.db_path, repository_root=self.repo)
        self.addCleanup(handle.close)
        return handle

    def restart(self) -> None:
        """Close the live handle and open a fresh one (a service restart)."""
        self.ledger.close()
        self.ledger = self._open()

    def register(self, *, content: str | None = None, owner_principal: str | None = None) -> int:
        return self.ledger.register_job(
            run_id="run-1",
            worktree=self.repo,
            owner="service",
            operator="operator",
            policy=_policy(),
            manifest_content=content if content is not None else _manifest(
                HUMAN_GATED, UNGATED
            ),
            owner_principal=owner_principal,
        )

    def credentials(self) -> endpoints.PeerCredentials:
        return endpoints.PeerCredentials(pid=os.getpid(), uid=os.getuid(), gid=os.getgid())

    def operator(self) -> broker.BrokerPrincipal:
        return broker.BrokerPrincipal(
            role=broker.OPERATOR, name="operator", uid=1000
        )

    def service(self) -> broker.BrokerPrincipal:
        return broker.BrokerPrincipal(
            role=broker.SERVICE, name="service", uid=0
        )

    def _write_plan_state(self, changes: dict) -> None:
        state_dir = self.repo / ".opsx-plan"
        state_dir.mkdir(parents=True, exist_ok=True)
        (state_dir / "supervision-test.state.json").write_text(
            json.dumps(
                {"plan": "supervision-test", "approvals": [], "changes": changes}
            ),
            encoding="utf-8",
        )

    def projection(self) -> dict:
        projection = supervision_mod.project_registered_job(
            self.repo, plan_name="supervision-test"
        )
        assert projection is not None
        return projection


class ProjectionFieldModelTests(SupervisionReportTestCase):
    def test_projection_exposes_the_full_field_model(self) -> None:
        job_id = self.register()
        self.ledger.set_job_state(job_id, "active")
        action_id = self.ledger.begin_action(job_id, kind="implement", run_id="run-1")
        self.ledger.dispatch_action(action_id)
        reservation_id = self.ledger.insert_reservation(
            job_id, action_id=action_id, role="implementer",
            requested_model="cheap/model-a",
            reserved_cost_usd=1.0, reserved_elapsed_minutes=2.0,
        )
        self.ledger.reconcile_reservation(
            reservation_id, observed_cost_usd=0.75, observed_elapsed_minutes=1.5
        )
        self.ledger.record_evidence(
            action_id, kind="stage_result",
            payload=json.dumps(
                {"confirmed": True, "outcome": "completed", "change_id": "gated-human"}
            ),
        )
        self.ledger.complete_action(action_id)
        self.ledger.record_wait(
            job_id, kind="human", change_id="gated-human",
            checkpoint="gate:approval:gated-human", material_hash="m",
        )
        self.ledger.record_incident(job_id, kind="transient_provider", summary="boom")
        recorded = broker.record_pause_or_steer(
            self.ledger, job_id, principal=self.service(),
            change_id="gated-human", kind="steer",
        )

        projection = self.projection()

        # Job identity, run linkage, state, progress timestamps, policy.
        self.assertEqual(projection["job_id"], job_id)
        self.assertEqual(projection["run_id"], "run-1")
        self.assertEqual(projection["state"], "active")
        self.assertTrue(projection["created_at"])
        self.assertTrue(projection["updated_at"])
        self.assertEqual(projection["policy"]["revision"], 1)
        # Actions carry their own identity, run linkage, and evidence.
        action = projection["recent_actions"][0]
        self.assertEqual(action["action_id"], action_id)
        self.assertEqual(action["run_id"], "run-1")
        self.assertEqual(action["evidence"][0]["kind"], "stage_result")
        # Incidents carry their own identity.
        incident = projection["recent_incidents"][0]
        self.assertEqual(incident["incident_id"], incident["id"])
        self.assertEqual(incident["kind"], "transient_provider")
        # Observed usage against protected limits.
        usage = projection["observed_usage"]
        self.assertAlmostEqual(usage["totals"]["reconciled_cost_usd"], 0.75)
        self.assertIn("reserved", usage["states"])
        self.assertEqual(usage["protected_limits"]["budgets"]["total_cost_usd"], 10.0)
        # Waits: the open human wait.
        self.assertEqual(projection["waits"][0]["kind"], "human")
        self.assertEqual(projection["waits"][0]["state"], "open")
        # Steering request with identity and acknowledgement state.
        steering = projection["steering_requests"][0]
        self.assertEqual(steering["request_id"], recorded.request_id)
        self.assertEqual(steering["ack_state"], "pending")
        self.assertIsNone(steering["ack_boundary"])
        # The metric definition travels with the projection.
        metric = projection["metrics"]["cost_per_correct_completion"]
        self.assertIn("definition", metric)
        self.assertIn("inputs", metric)
        self.assertIn("limitations", metric)

    def test_human_wait_briefing_records_evidence_and_approval(self) -> None:
        job_id = self.register()
        self.ledger.set_job_state(job_id, "active")
        action_id = self.ledger.begin_action(job_id, kind="review", run_id="run-1")
        self.ledger.record_evidence(
            action_id, kind="gate_request",
            payload=json.dumps({"change_id": "gated-human", "reason": "review passed"}),
        )
        self.ledger.record_wait(
            job_id, kind="human", change_id="gated-human",
            checkpoint="gate:approval:gated-human", material_hash="m",
        )

        projection = self.projection()

        briefing = projection["human_waits"][0]
        self.assertEqual(briefing["change_id"], "gated-human")
        self.assertEqual(briefing["checkpoint"], "gate:approval:gated-human")
        self.assertEqual(briefing["authority"], "human-only")
        self.assertIn("human-only", briefing["reason"])
        payloads = [json.dumps(item["payload"]) for item in briefing["evidence"]]
        self.assertTrue(any("review passed" in payload for payload in payloads))


class ProjectionReadOnlyTests(SupervisionReportTestCase):
    def test_projection_is_read_only_and_takes_no_lock(self) -> None:
        job_id = self.register()
        self.ledger.set_job_state(job_id, "active")
        self.ledger.record_wait(
            job_id, kind="human", change_id="gated-human",
            checkpoint="gate:approval:gated-human", material_hash="m",
        )
        self.ledger.close()
        before = self.db_path.read_bytes()

        projection = supervision_mod.project_registered_job(
            self.repo, plan_name="supervision-test"
        )
        self.assertIsNotNone(projection)
        self.assertEqual(self.db_path.read_bytes(), before)
        self.assertIsNone(lock.read_record(self.repo))
        self.ledger = self._open()

    def test_unregistered_worktree_yields_no_projection(self) -> None:
        self.assertIsNone(
            supervision_mod.project_registered_job(
                self.repo, plan_name="supervision-test"
            )
        )

    def test_terminal_job_is_still_projected(self) -> None:
        from lib.supervisor import lifecycle

        job_id = self.register()
        self.ledger.set_job_state(job_id, "active")
        lifecycle.cancel(self.ledger, job_id, authority="operator")
        projection = self.projection()
        self.assertEqual(projection["job_id"], job_id)
        self.assertEqual(projection["state"], "cancelled")


class SteeringRequestIdentityTests(SupervisionReportTestCase):
    def test_request_identity_acknowledged_once_and_survives_restart(self) -> None:
        job_id = self.register()
        recorded = broker.record_pause_or_steer(
            self.ledger, job_id, principal=self.service(),
            change_id="gated-human", kind="steer",
        )
        self.assertTrue(recorded.request_id.startswith("steer-"))
        self.assertEqual(recorded.ack_state, "pending")
        request_id = recorded.request_id

        # A service restart preserves the unacknowledged request.
        self.restart()
        row = self.ledger.get_receipt_by_request(job_id, request_id)
        self.assertEqual(row["request_id"], request_id)
        self.assertEqual(row["ack_state"], "pending")

        # The first acknowledgement records the boundary reached.
        acknowledged = self.ledger.acknowledge_request(
            job_id, request_id, boundary=ledger.ACK_BOUNDARY_CHANGE
        )
        self.assertEqual(acknowledged["ack_state"], "acknowledged")
        self.assertEqual(acknowledged["ack_boundary"], ledger.ACK_BOUNDARY_CHANGE)
        acked_at = acknowledged["acked_at"]

        # A duplicate acknowledgement is a no-op, never a second record.
        again = self.ledger.acknowledge_request(
            job_id, request_id, boundary=ledger.ACK_BOUNDARY_TERMINAL
        )
        self.assertEqual(again["ack_boundary"], ledger.ACK_BOUNDARY_CHANGE)
        self.assertEqual(again["acked_at"], acked_at)
        self.assertEqual(self.ledger.pending_acknowledgements(job_id), [])

    def test_pause_and_drain_acknowledge_at_the_stop_boundary(self) -> None:
        from lib.supervisor import lifecycle

        job_id = self.register()
        self.ledger.set_job_state(job_id, "active")
        lifecycle.pause(self.ledger, job_id, authority="operator")
        request = self.ledger.latest_steering_request(job_id)
        self.assertTrue(str(request["request_id"]).startswith("pause-"))
        self.assertEqual(request["ack_state"], "acknowledged")
        self.assertEqual(request["ack_boundary"], ledger.ACK_BOUNDARY_STOP)

        lifecycle.resume(self.ledger, job_id)
        lifecycle.drain(self.ledger, job_id, authority="operator")
        request = self.ledger.latest_steering_request(job_id)
        self.assertTrue(str(request["request_id"]).startswith("drain-"))
        self.assertEqual(request["ack_state"], "acknowledged")
        self.assertEqual(request["ack_boundary"], ledger.ACK_BOUNDARY_STOP)

    def test_cancel_acknowledges_at_the_terminal_boundary(self) -> None:
        from lib.supervisor import lifecycle

        job_id = self.register()
        self.ledger.set_job_state(job_id, "active")
        lifecycle.cancel(self.ledger, job_id, authority="operator")
        request = self.ledger.latest_steering_request(job_id)
        self.assertTrue(str(request["request_id"]).startswith("cancel-"))
        self.assertEqual(request["ack_state"], "acknowledged")
        self.assertEqual(request["ack_boundary"], ledger.ACK_BOUNDARY_TERMINAL)

    def test_pending_stop_request_is_reached_and_acked_after_restart(self) -> None:
        from lib.supervisor import lifecycle

        job_id = self.register()
        self.ledger.set_job_state(job_id, "active")
        # A drain with in-flight work stays pending until the hold is reached.
        action_id = self.ledger.begin_action(job_id, kind="implement", run_id="run-1")
        self.ledger.dispatch_action(action_id)
        lifecycle.drain(self.ledger, job_id, authority="operator")
        self.assertEqual(self.ledger.pending_acknowledgements(job_id)[0]["ack_state"],
                         "pending")

        self.restart()
        # A restart observes the durable hold once the action is terminal.
        self.ledger.complete_action(action_id)
        lifecycle.observe_stop_request(self.ledger, job_id)
        request = self.ledger.latest_steering_request(job_id)
        self.assertEqual(request["ack_state"], "acknowledged")
        self.assertEqual(request["ack_boundary"], ledger.ACK_BOUNDARY_STOP)
        self.assertEqual(self.ledger.get_job(job_id)["state"], "paused")

    def test_operator_retry_records_and_acknowledges_a_request(self) -> None:
        job_id = self.register()
        self.ledger.set_job_state(job_id, "active")

        result = endpoints._operator_reset_change(
            {
                "ledger": self.ledger,
                "job_id": job_id,
                "change_ids": ["open-change"],
            },
            self.credentials(),
        )

        request_id = result["request_ids"][0]
        self.assertTrue(request_id.startswith("reset-"))
        row = self.ledger.get_receipt_by_request(job_id, request_id)
        self.assertEqual(row["kind"], "reset")
        self.assertEqual(row["ack_state"], "acknowledged")
        self.assertEqual(row["ack_boundary"], ledger.ACK_BOUNDARY_CHANGE)
        self.assertEqual(result["acknowledgements"][0]["request_id"], request_id)
        self.assertEqual(self.ledger.pending_acknowledgements(job_id), [])
        # The reset request survives a restart and is never acknowledged twice.
        acked_at = row["acked_at"]
        self.restart()
        again = self.ledger.acknowledge_request(
            job_id, request_id, boundary=ledger.ACK_BOUNDARY_TERMINAL
        )
        self.assertEqual(again["ack_boundary"], ledger.ACK_BOUNDARY_CHANGE)
        self.assertEqual(again["acked_at"], acked_at)

    def test_operator_policy_revision_records_and_acknowledges_a_request(self) -> None:
        job_id = self.register()
        self.ledger.set_job_state(job_id, "active")
        current = self.ledger.current_policy(job_id)
        revised = {
            "authority_config": current["authority_config"],
            "model_selection": current["model_selection"],
            "inexpensive_allowlist": current["inexpensive_allowlist"],
            "manifest_snapshot_hash": current["manifest_snapshot_hash"],
            "budgets": {**current["budgets"], "total_cost_usd": 42.0},
            "deadlines": current["deadlines"],
        }

        result = endpoints._operator_revise_policy(
            {
                "ledger": self.ledger,
                "job_id": job_id,
                "revision": 2,
                "policy": revised,
            },
            self.credentials(),
        )

        request_id = result["request_id"]
        self.assertTrue(request_id.startswith("revise-"))
        self.assertEqual(result["ack_state"], "acknowledged")
        self.assertEqual(result["ack_boundary"], ledger.ACK_BOUNDARY_POLICY)
        row = self.ledger.get_receipt_by_request(job_id, request_id)
        self.assertEqual(row["kind"], "steer")
        self.assertEqual(row["ack_state"], "acknowledged")
        self.assertEqual(row["ack_boundary"], ledger.ACK_BOUNDARY_POLICY)
        # The revision was applied through the operator-only budget path.
        self.assertEqual(
            self.ledger.current_policy(job_id)["budgets"]["total_cost_usd"], 42.0
        )
        self.assertEqual(self.ledger.pending_acknowledgements(job_id), [])
        # A duplicate acknowledgement across a restart is a no-op.
        self.restart()
        again = self.ledger.acknowledge_request(
            job_id, request_id, boundary=ledger.ACK_BOUNDARY_CHANGE
        )
        self.assertEqual(again["ack_boundary"], ledger.ACK_BOUNDARY_POLICY)

    def _revised_policy(self) -> dict:
        current = self.ledger.current_policy(self._revision_job_id)
        return {
            "authority_config": current["authority_config"],
            "model_selection": current["model_selection"],
            "inexpensive_allowlist": current["inexpensive_allowlist"],
            "manifest_snapshot_hash": current["manifest_snapshot_hash"],
            "budgets": {**current["budgets"], "total_cost_usd": 42.0},
            "deadlines": current["deadlines"],
        }

    def test_invalid_policy_revision_leaves_no_phantom_request(self) -> None:
        job_id = self.register()
        self._revision_job_id = job_id
        self.ledger.set_job_state(job_id, "active")
        revised = self._revised_policy()

        # A wrong revision is refused before anything is recorded.
        with self.assertRaises(ledger.PolicyRevisionError):
            endpoints._operator_revise_policy(
                {
                    "ledger": self.ledger,
                    "job_id": job_id,
                    "revision": 7,
                    "policy": revised,
                },
                self.credentials(),
            )
        self.assertEqual(self.ledger.pending_acknowledgements(job_id), [])
        self.assertEqual(self.ledger.steering_requests(job_id), [])
        self.assertEqual(self.ledger.current_policy(job_id)["revision"], 1)

        # An invalid policy payload is likewise refused atomically.
        broken = {**revised, "budgets": {"version": 1}}
        with self.assertRaises(Exception):
            endpoints._operator_revise_policy(
                {
                    "ledger": self.ledger,
                    "job_id": job_id,
                    "revision": 2,
                    "policy": broken,
                },
                self.credentials(),
            )
        self.assertEqual(self.ledger.pending_acknowledgements(job_id), [])
        self.assertEqual(self.ledger.steering_requests(job_id), [])
        self.assertEqual(self.ledger.current_policy(job_id)["revision"], 1)

        # A restart still shows no phantom request and the original policy.
        self.restart()
        self.assertEqual(self.ledger.pending_acknowledgements(job_id), [])
        self.assertEqual(self.ledger.steering_requests(job_id), [])
        self.assertEqual(self.ledger.current_policy(job_id)["revision"], 1)

    def test_interrupted_policy_revision_leaves_no_phantom_request(self) -> None:
        job_id = self.register()
        self._revision_job_id = job_id
        self.ledger.set_job_state(job_id, "active")
        revised = self._revised_policy()

        # A crash inside the atomic transaction rolls back the revision, the
        # receipt, and its acknowledgement together.
        with mock.patch.object(
            self.ledger,
            "_insert_policy",
            side_effect=RuntimeError("simulated crash mid-revision"),
        ):
            with self.assertRaises(RuntimeError):
                endpoints._operator_revise_policy(
                    {
                        "ledger": self.ledger,
                        "job_id": job_id,
                        "revision": 2,
                        "policy": revised,
                    },
                    self.credentials(),
                )
        self.assertEqual(self.ledger.pending_acknowledgements(job_id), [])
        self.assertEqual(self.ledger.steering_requests(job_id), [])
        self.assertEqual(self.ledger.current_policy(job_id)["revision"], 1)

        # The request leaves nothing to resume across a restart: no unapplied
        # pending request exists, and a valid retry applies cleanly.
        self.restart()
        self.assertEqual(self.ledger.pending_acknowledgements(job_id), [])
        result = endpoints._operator_revise_policy(
            {
                "ledger": self.ledger,
                "job_id": job_id,
                "revision": 2,
                "policy": revised,
            },
            self.credentials(),
        )
        self.assertEqual(result["ack_state"], "acknowledged")
        self.assertEqual(result["ack_boundary"], ledger.ACK_BOUNDARY_POLICY)
        self.assertEqual(
            self.ledger.current_policy(job_id)["budgets"]["total_cost_usd"], 42.0
        )


class NotificationDedupTests(SupervisionReportTestCase):
    def test_reboot_does_not_replay_a_delivered_notification(self) -> None:
        job_id = self.register()
        first = broker.record_pause_or_steer(
            self.ledger, job_id, principal=self.service(),
            change_id="gated-human", kind="steer",
        )
        batch = broker.select_steering_notifications(self.ledger, job_id)
        self.assertEqual([row["id"] for row in batch.receipts], [first.receipt_id])
        self.assertEqual(batch.high_water, first.receipt_id)
        # Selection alone never advances the durable cursor.
        self.assertEqual(self.ledger.notification_watermark(job_id, "worker"), 0)
        broker.acknowledge_notification_delivery(
            self.ledger, job_id, high_water=batch.high_water
        )

        # A reboot resumes from the persisted watermark: no replay.
        self.restart()
        replay = broker.select_steering_notifications(self.ledger, job_id)
        self.assertEqual(replay.receipts, [])
        self.assertEqual(replay.cursor, first.receipt_id)

        # A new notification above the watermark is delivered exactly once.
        second = broker.record_pause_or_steer(
            self.ledger, job_id, principal=self.operator(),
            change_id="gated-human", kind="pause",
        )
        fresh = broker.select_steering_notifications(self.ledger, job_id)
        self.assertEqual([row["id"] for row in fresh.receipts], [second.receipt_id])
        broker.acknowledge_notification_delivery(
            self.ledger, job_id, high_water=fresh.high_water
        )
        self.assertEqual(
            broker.select_steering_notifications(self.ledger, job_id).receipts, []
        )

    def test_replay_after_delivery_returns_nothing(self) -> None:
        job_id = self.register()
        broker.record_pause_or_steer(
            self.ledger, job_id, principal=self.service(),
            change_id="gated-human", kind="steer",
        )
        batch = broker.select_steering_notifications(self.ledger, job_id)
        broker.acknowledge_notification_delivery(
            self.ledger, job_id, high_water=batch.high_water
        )
        replay = broker.select_steering_notifications(self.ledger, job_id)
        self.assertEqual(replay.receipts, [])

    def test_oversized_caller_high_water_still_delivers_pending_receipts(self) -> None:
        job_id = self.register(owner_principal="opsx-service")
        recorded = broker.record_pause_or_steer(
            self.ledger, job_id, principal=self.service(),
            change_id="gated-human", kind="steer",
        )
        request = {
            "verb": "request_action",
            "service_identity": "opsx-service",
            "role": "implementer",
            "observed_agent": "opsx-implementer",
            # A caller offset beyond every receipt must not suppress delivery:
            # the persisted consumer watermark is the sole delivery cursor.
            "high_water": recorded.receipt_id + 1000,
        }

        deferred = endpoints._worker_request_action(
            dict(request, ledger=self.ledger, job_id=job_id), self.credentials()
        )

        self.assertIsInstance(deferred, endpoints.DeferredDelivery)
        self.assertEqual(
            [item["receipt_id"] for item in deferred.result["steering_receipts"]],
            [recorded.receipt_id],
        )
        # Selection alone still left the durable watermark untouched.
        self.assertEqual(self.ledger.notification_watermark(job_id, "worker"), 0)

    def test_delivery_failure_then_reboot_redelivers(self) -> None:
        job_id = self.register()
        recorded = broker.record_pause_or_steer(
            self.ledger, job_id, principal=self.service(),
            change_id="gated-human", kind="steer",
        )
        # Delivery fails: selection happened but the acknowledgement never did.
        batch = broker.select_steering_notifications(self.ledger, job_id)
        self.assertEqual([row["id"] for row in batch.receipts], [recorded.receipt_id])
        with mock.patch.object(
            self.ledger, "advance_notification_watermark",
            side_effect=RuntimeError("delivery backend down"),
        ):
            with self.assertRaises(RuntimeError):
                broker.acknowledge_notification_delivery(
                    self.ledger, job_id, high_water=batch.high_water
                )
        self.assertEqual(self.ledger.notification_watermark(job_id, "worker"), 0)

        # A reboot/retry redelivers instead of suppressing the notification.
        self.restart()
        retry = broker.select_steering_notifications(self.ledger, job_id)
        self.assertEqual([row["id"] for row in retry.receipts], [recorded.receipt_id])
        broker.acknowledge_notification_delivery(
            self.ledger, job_id, high_water=retry.high_water
        )
        self.assertEqual(self.ledger.notification_watermark(job_id, "worker"),
                         recorded.receipt_id)

    def test_notification_failure_does_not_lose_a_gate(self) -> None:
        job_id = self.register()
        approved = broker.record_approval(
            self.ledger, job_id, principal=self.operator(),
            change_ids=["gated-human"],
        )
        self.assertEqual(len(approved), 1)

        # Delivery fails, but the durable approval already satisfies the gate.
        batch = broker.select_steering_notifications(self.ledger, job_id)
        with mock.patch.object(
            self.ledger, "advance_notification_watermark",
            side_effect=RuntimeError("notification backend down"),
        ):
            with self.assertRaises(RuntimeError):
                broker.acknowledge_notification_delivery(
                    self.ledger, job_id, high_water=batch.high_water
                )
        resolution = broker.resolve_gate(self.ledger, job_id, "gated-human")
        self.assertTrue(resolution.dispatchable)
        self.assertEqual(
            self.ledger.get_receipt(approved[0].receipt_id)["kind"], "approval"
        )

    def test_endpoint_delivery_failure_leaves_the_watermark_pending(self) -> None:
        job_id = self.register(owner_principal="opsx-service")
        recorded = broker.record_pause_or_steer(
            self.ledger, job_id, principal=self.service(),
            change_id="gated-human", kind="steer",
        )
        request = {
            "verb": "request_action",
            "service_identity": "opsx-service",
            "role": "implementer",
            "observed_agent": "opsx-implementer",
        }

        # The real worker handler defers the acknowledgement to the transport.
        deferred = endpoints._worker_request_action(
            dict(request, ledger=self.ledger, job_id=job_id), self.credentials()
        )
        self.assertIsInstance(deferred, endpoints.DeferredDelivery)
        self.assertEqual(
            [item["receipt_id"] for item in deferred.result["steering_receipts"]],
            [recorded.receipt_id],
        )
        self.assertEqual(self.ledger.notification_watermark(job_id, "worker"), 0)

        # A response-write failure is not acknowledged and a reboot redelivers.
        self.assertRaises(
            OSError,
            broker_client.serve_one,
            endpoints.Endpoint(
                endpoints.ENDPOINT_WORKER, frozenset({os.getuid()})
            ),
            _FakeConn(broker_client.encode_request(request), fail_write=True),
            ledger_resolver=lambda _request: (self.ledger, job_id),
        )
        self.assertEqual(self.ledger.notification_watermark(job_id, "worker"), 0)
        self.restart()
        retry = broker.select_steering_notifications(self.ledger, job_id)
        self.assertEqual([row["id"] for row in retry.receipts], [recorded.receipt_id])


class _FakeConn:
    """A minimal server-side connection with controllable write behavior."""

    def __init__(self, request: bytes, *, fail_write: bool = False) -> None:
        self._request = request
        self._read = False
        self._fail_write = fail_write
        self.written: list[bytes] = []

    def getsockopt(self, level: int, option: int, size: int) -> bytes:
        return struct.pack("3i", os.getpid(), os.getuid(), os.getgid())

    def recv(self, size: int) -> bytes:
        if self._read:
            return b""
        self._read = True
        return self._request

    def sendall(self, data: bytes) -> None:
        if self._fail_write:
            raise OSError("peer went away before the response was delivered")
        self.written.append(data)

    def close(self) -> None:
        pass


class IdentityLinkageTests(SupervisionReportTestCase):
    def test_supervision_identities_are_distinct_but_linked_to_run_id(self) -> None:
        job_id = self.register()
        action_id = self.ledger.begin_action(job_id, kind="implement", run_id="run-1")
        incident_id = self.ledger.record_incident(
            job_id, kind="transient_provider", summary="boom", run_id="run-1"
        )
        recorded = broker.record_pause_or_steer(
            self.ledger, job_id, principal=self.service(),
            change_id="gated-human", kind="steer",
        )
        # Identifiers live in their own namespaces and are reported as their
        # own fields; none of them is the plan run identity.
        self.assertNotEqual(str(recorded.request_id), "run-1")
        self.assertEqual(
            self.ledger.get_receipt_by_request(job_id, recorded.request_id)["job_id"],
            job_id,
        )
        self.assertEqual(self.ledger.get_action(action_id)["job_id"], job_id)
        self.assertEqual(self.ledger.get_incident(incident_id)["run_id"], "run-1")

        projection = self.projection()
        self.assertEqual(projection["job_id"], job_id)
        self.assertEqual(projection["run_id"], "run-1")
        self.assertEqual(projection["recent_actions"][0]["action_id"], action_id)
        self.assertEqual(projection["recent_actions"][0]["run_id"], "run-1")
        self.assertEqual(
            projection["recent_incidents"][0]["incident_id"], incident_id
        )
        self.assertEqual(
            projection["steering_requests"][0]["request_id"], recorded.request_id
        )


class CostPerCorrectCompletionTests(SupervisionReportTestCase):
    def _project_metric(self) -> dict:
        return self.projection()["metrics"]["cost_per_correct_completion"]

    def test_value_is_undefined_without_a_correct_completion(self) -> None:
        self.register()
        metric = self._project_metric()
        self.assertIsNone(metric["value"])
        self.assertEqual(metric["inputs"]["correct_completions"], 0)

    def test_value_is_reconciled_cost_over_correct_completions(self) -> None:
        job_id = self.register()
        self.ledger.set_job_state(job_id, "active")
        action_id = self.ledger.begin_action(job_id, kind="implement", run_id="run-1")
        reservation_id = self.ledger.insert_reservation(
            job_id, action_id=action_id, role="implementer",
            requested_model="cheap/model-a",
            reserved_cost_usd=2.0, reserved_elapsed_minutes=1.0,
        )
        self.ledger.reconcile_reservation(reservation_id, observed_cost_usd=1.5)
        self._write_plan_state({"gated-human": {"status": "done"}})

        metric = self._project_metric()
        self.assertEqual(metric["inputs"]["completed_changes"], 1)
        self.assertEqual(metric["inputs"]["rework_incidents"], 0)
        self.assertEqual(metric["inputs"]["correct_completions"], 1)
        self.assertAlmostEqual(metric["value"], 1.5)

    def test_definition_carries_limitations_and_no_performance_claim(self) -> None:
        self.register()
        metric = self._project_metric()
        self.assertIn("definition", metric)
        self.assertEqual(set(metric), {"value", "definition", "inputs", "limitations"})
        self.assertTrue(metric["limitations"])
        text = " ".join(metric["limitations"]).lower()
        self.assertIn("not a benchmark", text)
        self.assertIn("promise", text)
        # The metric is a definition, never a savings or performance assertion.
        for forbidden in ("saves", "faster", "guarantees", "improves"):
            self.assertNotIn(forbidden, text)


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
