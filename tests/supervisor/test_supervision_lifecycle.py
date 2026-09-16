"""Lifecycle and migration tests for the supervised job surface.

Covers the durable state machine (``register``/``start``/``resume``/``pause``/
``drain``/``cancel``/``complete``), the persisted registration record, the two
stop boundaries and their restart survival, cancellation effects, human waits,
evidence-based completion, the operator endpoint lifecycle verbs, and the
forward-only schema migration that adds the ``waits`` table, the ``drain``
receipt kind, and the registration linkage configuration.
"""

from __future__ import annotations

import hashlib
import sqlite3
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from lib.supervisor import authority
from lib.supervisor import broker as broker_mod
from lib.supervisor import budgets
from lib.supervisor import endpoints
from lib.supervisor import lifecycle
from lib.supervisor import ledger
from lib.supervisor import model_policy

MANIFEST = (
    "[[changes]]\n"
    'id = "gated-human"\n'
    "pause_before = true\n"
    "depends_on = []\n"
    "\n"
    "[[changes]]\n"
    'id = "ungated"\n'
    "pause_before = false\n"
    "depends_on = []\n"
)


def _selection() -> dict:
    roles = {
        role: "openai/gpt-4o" for role in model_policy.POLICY_ROLES
    }
    return {
        "version": model_policy.MODEL_POLICY_VERSION,
        "roles": roles,
        "stages": dict(model_policy.STANDARD_STAGE_MAPPING),
    }


def _policy(**overrides) -> dict:
    base = {
        "authority_config": {"mode": "policy-bound", "approval": "supervisor"},
        "model_selection": _selection(),
        "inexpensive_allowlist": {
            "version": model_policy.MODEL_POLICY_VERSION,
            "models": ["openai/gpt-4o"],
            "source": "test fixture",
        },
        "manifest_snapshot_hash": "deadbeef",
        "budgets": {
            "version": budgets.BUDGET_SCHEMA_VERSION,
            "total_cost_usd": 100.0,
            "per_action_cost_usd": None,
            "total_elapsed_minutes": None,
            "per_action_elapsed_minutes": None,
            "max_incident_attempts": 3,
        },
        "deadlines": {
            "version": budgets.BUDGET_SCHEMA_VERSION,
            "execution_deadline_minutes": None,
        },
    }
    base.update(overrides)
    return base


class LifecycleTestCase(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.root = Path(self._tmp.name)
        self.repo = self.root / "repo"
        self.repo.mkdir()
        (self.repo / ".git").mkdir()
        self.worktree = self.repo / "worktree"
        self.worktree.mkdir()
        self.storage = self.root / "service-storage"
        self.storage.mkdir()
        self.db_path = self.storage / "supervisor.sqlite3"
        self.ledger = self.open()

    def open(self, **kwargs: object) -> ledger.Ledger:
        kwargs.setdefault("repository_root", self.repo)
        handle = ledger.open_ledger(self.db_path, **kwargs)
        self.addCleanup(handle.close)
        return handle

    def register_job(self, handle: ledger.Ledger, **overrides: object) -> int:
        params = {
            "run_id": "run-1",
            "worktree": self.worktree,
            "owner": "service",
            "policy": _policy(),
            "operator": "operator",
            "manifest_content": MANIFEST,
            "linkage_config": {"adapter": "opencode", "primary_session": True},
        }
        params.update(overrides)
        return handle.register_job(**params)

    def active_job(self) -> int:
        job_id = self.register_job(self.ledger)
        lifecycle.start(self.ledger, job_id)
        return job_id

    def start_in_flight_action(self, job_id: int) -> int:
        action_id = self.ledger.begin_action(job_id, kind="implement", run_id="run-1")
        self.ledger.dispatch_action(action_id, session_id="session-1")
        return action_id


class RegistrationTests(LifecycleTestCase):
    def test_register_persists_the_complete_record(self) -> None:
        with mock.patch.object(
            authority, "require_authority_backend", return_value=mock.Mock()
        ) as gate:
            job_id = lifecycle.register(
                self.ledger,
                worktree=self.worktree,
                repository_root=self.repo,
                owner="operator:1000",
                operator="1000",
                policy=_policy(),
                manifest_content=MANIFEST,
                run_id="run-1",
                linkage_config={"adapter": "opencode", "primary_session": True},
            )
        self.assertEqual(gate.call_count, 1)
        job = self.ledger.get_job(job_id)
        self.assertEqual(job["state"], "registered")
        self.assertEqual(job["owner"], "operator:1000")
        policy = self.ledger.current_policy(job_id)
        self.assertEqual(policy["revision"], 1)
        self.assertEqual(
            policy["manifest_snapshot_hash"], ledger.snapshot_digest(MANIFEST)
        )
        self.assertEqual(
            self.ledger.manifest_snapshot(job_id, policy["manifest_snapshot_hash"]),
            MANIFEST,
        )
        self.assertEqual(
            self.ledger.job_linkage_config(job_id),
            {"adapter": "opencode", "primary_session": True},
        )
        self.assertIn("model_selection", policy)
        self.assertIn("budgets", policy)

    def test_register_fails_closed_without_a_supported_backend(self) -> None:
        boom = authority.UnsupportedHostError("no supported authority backend")
        with mock.patch.object(
            authority, "require_authority_backend", side_effect=boom
        ):
            with self.assertRaises(authority.UnsupportedHostError):
                lifecycle.register(
                    self.ledger,
                    worktree=self.worktree,
                    owner="operator",
                    operator="operator",
                    policy=_policy(),
                    manifest_content=MANIFEST,
                    run_id="run-1",
                )
        self.assertEqual(self.ledger.list_jobs(), [])

    def test_register_refuses_an_unparseable_manifest(self) -> None:
        with mock.patch.object(
            authority, "require_authority_backend", return_value=mock.Mock()
        ):
            with self.assertRaises(lifecycle.LifecycleError):
                lifecycle.register(
                    self.ledger,
                    worktree=self.worktree,
                    owner="operator",
                    operator="operator",
                    policy=_policy(),
                    manifest_content="not a manifest = = =",
                    run_id="run-1",
                )
        self.assertEqual(self.ledger.list_jobs(), [])

    def test_lifecycle_command_on_an_unregistered_worktree_is_unknown_job(self) -> None:
        for verb in (
            lifecycle.start,
            lifecycle.resume,
            lifecycle.pause,
            lifecycle.drain,
            lifecycle.cancel,
        ):
            with self.assertRaises(lifecycle.UnknownJobError):
                lifecycle.job_for_worktree(
                    self.ledger, self.worktree, repository_root=self.repo
                )
            with self.assertRaises(lifecycle.UnknownJobError):
                verb(self.ledger, 999)


class StateMachineTests(LifecycleTestCase):
    def test_start_activates_a_registered_job_durably(self) -> None:
        job_id = self.register_job(self.ledger)
        job = lifecycle.start(self.ledger, job_id)
        self.assertEqual(job["state"], "active")
        reopened = ledger.open_ledger(
            self.db_path, repository_root=self.repo, create=False
        )
        self.addCleanup(reopened.close)
        self.assertEqual(reopened.get_job(job_id)["state"], "active")

    def test_resume_revalidates_before_activating(self) -> None:
        job_id = self.active_job()
        lifecycle.pause(self.ledger, job_id)
        job = lifecycle.resume(self.ledger, job_id)
        self.assertEqual(job["state"], "active")
        self.assertEqual(self.ledger.open_waits(job_id, kind="stop"), [])

    def test_resume_with_a_stale_receipt_re_arms_the_gate(self) -> None:
        job_id = self.active_job()
        # Approve the human gate, then bump the policy revision so the receipt
        # no longer matches the material revision.
        approval = self._approve_gate(job_id, "gated-human")
        self.assertIsNotNone(approval)
        lifecycle.pause(self.ledger, job_id)
        current = self.ledger.current_policy(job_id)
        self.ledger.revise_policy(
            job_id,
            revision=int(current["revision"]) + 1,
            policy=_policy(),
            operator="operator",
        )
        with self.assertRaises(broker_mod.StaleMaterialError):
            lifecycle.resume(self.ledger, job_id)
        self.assertEqual(self.ledger.get_job(job_id)["state"], "paused")

    def _approve_gate(self, job_id: int, change_id: str) -> int | None:
        material = broker_mod.material_state(self.ledger, job_id, change_id)
        digest = broker_mod.material_hash(
            change_id,
            material.fields,
            material.snapshot_hash,
            material.policy_revision,
        )
        return self.ledger.record_receipt(
            job_id,
            change_id=change_id,
            kind="approval",
            checkpoint=broker_mod.checkpoint_for("approval", change_id),
            material_hash=digest,
            authority="operator",
        )

    def test_illegal_transition_is_refused_and_unchanged(self) -> None:
        job_id = self.register_job(self.ledger)
        with self.assertRaises(lifecycle.IllegalTransitionError):
            lifecycle.resume(self.ledger, job_id)
        self.assertEqual(self.ledger.get_job(job_id)["state"], "registered")
        lifecycle.start(self.ledger, job_id)
        with self.assertRaises(lifecycle.IllegalTransitionError):
            lifecycle.start(self.ledger, job_id)
        self.assertEqual(self.ledger.get_job(job_id)["state"], "active")
        lifecycle.pause(self.ledger, job_id)
        with self.assertRaises(lifecycle.IllegalTransitionError):
            lifecycle.pause(self.ledger, job_id)
        self.assertEqual(self.ledger.get_job(job_id)["state"], "paused")

    def test_terminal_job_refuses_mutation(self) -> None:
        job_id = self.register_job(self.ledger)
        lifecycle.cancel(self.ledger, job_id)
        before = dict(self.ledger.get_job(job_id))
        for verb in (lifecycle.start, lifecycle.resume, lifecycle.pause,
                     lifecycle.drain, lifecycle.cancel):
            with self.assertRaises(lifecycle.TerminalJobError):
                verb(self.ledger, job_id)
        self.assertEqual(dict(self.ledger.get_job(job_id)), before)

    def test_complete_requires_an_active_job(self) -> None:
        job_id = self.active_job()
        self.assertEqual(lifecycle.complete(self.ledger, job_id)["state"], "completed")
        with self.assertRaises(lifecycle.TerminalJobError):
            lifecycle.complete(self.ledger, job_id)


class StopBoundaryTests(LifecycleTestCase):
    def test_pause_interrupts_in_flight_work(self) -> None:
        job_id = self.active_job()
        action_id = self.start_in_flight_action(job_id)
        job = lifecycle.pause(self.ledger, job_id)
        self.assertEqual(job["state"], "paused")
        self.assertEqual(self.ledger.get_action(action_id)["state"], "uncertain")
        receipts = [row for row in self.ledger.stop_receipts(job_id)]
        self.assertEqual([row["kind"] for row in receipts], ["pause"])
        self.assertEqual(len(self.ledger.open_waits(job_id, kind="stop")), 1)

    def test_drain_lets_in_flight_work_finish(self) -> None:
        job_id = self.active_job()
        action_id = self.start_in_flight_action(job_id)
        job = lifecycle.drain(self.ledger, job_id)
        # In-flight work keeps the job active under the durable hold, and no new
        # dispatch is permitted.
        self.assertEqual(job["state"], "active")
        stop = lifecycle.observe_stop_request(self.ledger, job_id)
        self.assertEqual(stop["disposition"], "draining")
        self.assertFalse(stop["dispatch_allowed"])
        self.assertEqual(self.ledger.get_action(action_id)["state"], "dispatched")
        # Once the in-flight action reaches a terminal outcome, the boundary
        # records the paused state.
        self.ledger.complete_action(action_id)
        stop = lifecycle.observe_stop_request(self.ledger, job_id)
        self.assertEqual(stop["disposition"], "paused")
        self.assertEqual(self.ledger.get_job(job_id)["state"], "paused")

    def test_drain_with_no_in_flight_work_pauses_immediately(self) -> None:
        job_id = self.active_job()
        job = lifecycle.drain(self.ledger, job_id)
        self.assertEqual(job["state"], "paused")

    def test_stop_request_survives_a_restart(self) -> None:
        job_id = self.active_job()
        self.start_in_flight_action(job_id)
        lifecycle.drain(self.ledger, job_id)
        # Simulate a restart: a fresh ledger handle sees only durable state.
        reopened = ledger.open_ledger(
            self.db_path, repository_root=self.repo, create=False
        )
        self.addCleanup(reopened.close)
        self.assertTrue(lifecycle.stop_request_pending(reopened, job_id))
        stop = lifecycle.observe_stop_request(reopened, job_id)
        self.assertFalse(stop["dispatch_allowed"])
        self.assertEqual(stop["disposition"], "draining")
        self.assertTrue(self.ledger.stop_receipts(job_id))

    def test_stop_request_needs_no_execution_lock(self) -> None:
        # No repo lock record exists; pause is still legal and durable.
        job_id = self.active_job()
        self.assertFalse((self.repo / ".opsx-plan" / "execution.lock").exists())
        lifecycle.pause(self.ledger, job_id)
        self.assertEqual(self.ledger.get_job(job_id)["state"], "paused")

    def test_pause_and_drain_are_refused_outside_active(self) -> None:
        job_id = self.register_job(self.ledger)
        for verb in (lifecycle.pause, lifecycle.drain):
            with self.assertRaises(lifecycle.IllegalTransitionError):
                verb(self.ledger, job_id)
        self.assertEqual(self.ledger.get_job(job_id)["state"], "registered")


class CancellationTests(LifecycleTestCase):
    def test_cancel_records_terminal_state_with_in_flight_disposition(self) -> None:
        job_id = self.active_job()
        intent_id = self.ledger.begin_action(job_id, kind="implement", run_id="run-1")
        dispatched_id = self.start_in_flight_action(job_id)
        job = lifecycle.cancel(self.ledger, job_id)
        self.assertEqual(job["state"], "cancelled")
        self.assertEqual(self.ledger.get_action(intent_id)["state"], "failed")
        self.assertIn(
            "cancelled", str(self.ledger.get_action(intent_id)["detail"] or "")
        )
        self.assertEqual(
            self.ledger.get_action(dispatched_id)["state"], "uncertain"
        )

    def test_new_registration_is_legal_after_cancellation(self) -> None:
        first = self.active_job()
        lifecycle.cancel(self.ledger, first)
        second = self.register_job(self.ledger, run_id="run-2")
        self.assertNotEqual(first, second)
        self.assertEqual(self.ledger.get_job(first)["state"], "cancelled")

    def test_cancelled_job_refuses_further_requests(self) -> None:
        job_id = self.active_job()
        lifecycle.cancel(self.ledger, job_id)
        # A stop request is refused.
        with self.assertRaises(lifecycle.TerminalJobError):
            lifecycle.pause(self.ledger, job_id)
        # A lifecycle verb is refused.
        with self.assertRaises(lifecycle.TerminalJobError):
            lifecycle.resume(self.ledger, job_id)
        self.assertEqual(self.ledger.get_job(job_id)["state"], "cancelled")


class HumanWaitTests(LifecycleTestCase):
    def _journal(self):
        from lib.orchestrator import journal_dispatch

        return journal_dispatch

    def test_human_only_gate_records_a_durable_wait(self) -> None:
        job_id = self.active_job()
        with self.assertRaises(self._journal().AuthorityGateError):
            self._journal().assert_authority_gate(
                self.ledger, job_id, "gated-human"
            )
        wait = lifecycle.open_human_wait(self.ledger, job_id, change_id="gated-human")
        self.assertIsNotNone(wait)
        self.assertEqual(str(wait["checkpoint"]), "approval:gated-human")

    def test_wait_survives_a_restart_and_dispatches_nothing(self) -> None:
        job_id = self.active_job()
        with self.assertRaises(self._journal().AuthorityGateError):
            self._journal().assert_authority_gate(
                self.ledger, job_id, "gated-human"
            )
        actions_before = len(self.ledger.list_actions(job_id))
        reopened = ledger.open_ledger(
            self.db_path, repository_root=self.repo, create=False
        )
        self.addCleanup(reopened.close)
        wait = lifecycle.open_human_wait(reopened, job_id, change_id="gated-human")
        self.assertIsNotNone(wait)
        self.assertEqual(len(reopened.list_actions(job_id)), actions_before)

    def test_approval_receipt_ends_the_wait(self) -> None:
        job_id = self.active_job()
        with self.assertRaises(self._journal().AuthorityGateError):
            self._journal().assert_authority_gate(
                self.ledger, job_id, "gated-human"
            )
        material = broker_mod.material_state(self.ledger, job_id, "gated-human")
        digest = broker_mod.material_hash(
            "gated-human",
            material.fields,
            material.snapshot_hash,
            material.policy_revision,
        )
        self.ledger.record_receipt(
            job_id,
            change_id="gated-human",
            kind="approval",
            checkpoint=broker_mod.checkpoint_for("approval", "gated-human"),
            material_hash=digest,
            authority="operator",
        )
        self._journal().assert_authority_gate(self.ledger, job_id, "gated-human")
        self.assertIsNone(
            lifecycle.open_human_wait(self.ledger, job_id, change_id="gated-human")
        )


class EndpointLifecycleTests(LifecycleTestCase):
    """The operator endpoint verbs share the lifecycle transition table."""

    def credentials(self) -> endpoints.PeerCredentials:
        return endpoints.PeerCredentials(pid=1, uid=1000, gid=1000)

    def run_verb(self, verb: str, job_id: int, **payload) -> dict:
        request = {"verb": verb, "job_id": job_id, "ledger": self.ledger}
        request.update(payload)
        return endpoints.OPERATOR_HANDLERS[verb](request, self.credentials())

    def test_pause_drain_resume_and_cancel_are_reachable(self) -> None:
        job_id = self.active_job()
        result = self.run_verb("pause", job_id)
        self.assertEqual(result["state"], "paused")
        result = self.run_verb("resume", job_id)
        self.assertEqual(result["state"], "active")
        result = self.run_verb("drain", job_id)
        self.assertEqual(result["state"], "paused")
        result = self.run_verb("cancel", job_id)
        self.assertEqual(result["state"], "cancelled")

    def test_cancelled_job_refuses_receipts_through_the_endpoint(self) -> None:
        job_id = self.active_job()
        self.run_verb("cancel", job_id)
        with self.assertRaises(lifecycle.TerminalJobError):
            self.run_verb("approve", job_id, change_ids=["gated-human"])

    def test_operator_and_worker_tables_stay_disjoint(self) -> None:
        self.assertTrue(endpoints.handler_tables_are_disjoint())
        for verb in ("pause", "drain", "resume", "cancel"):
            self.assertIn(verb, endpoints.OPERATOR_HANDLERS)


class CompletionTests(unittest.TestCase):
    """Supervised completion is decided from plan/archive/check evidence."""

    def setUp(self) -> None:
        import importlib.util
        import sys

        script = Path(__file__).resolve().parents[2] / "orchestrator" / "opsx-plan.py"
        spec = importlib.util.spec_from_file_location("opsx_plan", script)
        assert spec is not None and spec.loader is not None
        module = importlib.util.module_from_spec(spec)
        sys.modules["opsx_plan"] = module
        spec.loader.exec_module(module)
        self.opsx_plan = module

    def test_completion_requires_archive_and_check_evidence(self) -> None:
        cfg = {
            "name": "plan",
            "order": ["change-a"],
            "changes": {"change-a": {"enabled": True}},
        }
        with mock.patch.object(
            self.opsx_plan.state_mod, "load_state", return_value={"changes": {}}
        ), mock.patch.object(
            self.opsx_plan, "verify_direct_archive_done", return_value=(True, "")
        ), mock.patch.object(
            self.opsx_plan.state_mod, "pending_manual_tasks", return_value=[]
        ), mock.patch.object(
            self.opsx_plan.groundtruth, "run_fast_checks", return_value=(True, "")
        ), mock.patch.object(
            self.opsx_plan.delivery, "verify_post_archive_clean",
            return_value=(True, ""),
        ):
            complete, failures, manual = self.opsx_plan.evaluate_supervised_completion(
                Path("."), cfg, None, 1
            )
        self.assertTrue(complete)
        self.assertEqual(failures, [])
        self.assertEqual(manual, {})

    def test_worker_claim_does_not_complete_without_evidence(self) -> None:
        cfg = {
            "name": "plan",
            "order": ["change-a"],
            "changes": {"change-a": {"enabled": True}},
        }
        with mock.patch.object(
            self.opsx_plan.state_mod, "load_state", return_value={"changes": {}}
        ), mock.patch.object(
            self.opsx_plan, "verify_direct_archive_done",
            return_value=(False, "archive missing"),
        ), mock.patch.object(
            self.opsx_plan.state_mod, "pending_manual_tasks", return_value=[]
        ):
            complete, failures, _ = self.opsx_plan.evaluate_supervised_completion(
                Path("."), cfg, None, 1
            )
        self.assertFalse(complete)
        self.assertEqual(failures[0]["change_id"], "change-a")

    def test_failed_fast_check_blocks_completion(self) -> None:
        cfg = {
            "name": "plan",
            "order": ["change-a"],
            "changes": {"change-a": {"enabled": True}},
        }
        with mock.patch.object(
            self.opsx_plan.state_mod, "load_state", return_value={"changes": {}}
        ), mock.patch.object(
            self.opsx_plan, "verify_direct_archive_done", return_value=(True, "")
        ), mock.patch.object(
            self.opsx_plan.state_mod, "pending_manual_tasks", return_value=[]
        ), mock.patch.object(
            self.opsx_plan.groundtruth, "run_fast_checks",
            return_value=(False, "check failed: smoke"),
        ):
            complete, failures, _ = self.opsx_plan.evaluate_supervised_completion(
                Path("."), cfg, None, 1
            )
        self.assertFalse(complete)
        self.assertIn("check failed", failures[0]["reason"])

    def test_pending_manual_tasks_are_a_checklist_not_a_failure(self) -> None:
        cfg = {
            "name": "plan",
            "order": ["change-a"],
            "changes": {"change-a": {"enabled": True}},
        }
        with mock.patch.object(
            self.opsx_plan.state_mod, "load_state", return_value={"changes": {}}
        ), mock.patch.object(
            self.opsx_plan, "verify_direct_archive_done", return_value=(True, "")
        ), mock.patch.object(
            self.opsx_plan.state_mod, "pending_manual_tasks",
            return_value=["1.2 Plant fixtures (manual)"],
        ), mock.patch.object(
            self.opsx_plan.groundtruth, "run_fast_checks", return_value=(True, "")
        ), mock.patch.object(
            self.opsx_plan.delivery, "verify_post_archive_clean",
            return_value=(True, ""),
        ):
            complete, failures, manual = self.opsx_plan.evaluate_supervised_completion(
                Path("."), cfg, None, 1
            )
        self.assertTrue(complete)
        self.assertEqual(failures, [])
        self.assertEqual(manual, {"change-a": ["1.2 Plant fixtures (manual)"]})


class MigrationTests(LifecycleTestCase):
    """The version-5 to version-6 migration adds waits, drain, and linkage."""

    @unittest.skipUnless(
        sqlite3.sqlite_version_info >= (3, 35, 0),
        "ALTER TABLE ... DROP COLUMN requires SQLite 3.35+",
    )
    def test_v5_ledger_migrates_forward_preserving_records(self) -> None:
        job_id = self.register_job(self.ledger)
        self.ledger.record_receipt(
            job_id,
            change_id="ungated",
            kind="approval",
            checkpoint="gate:approval:ungated",
            material_hash="material",
            authority="operator",
        )
        self.ledger.close()

        # Simulate a genuine v5 ledger: no waits table, no linkage column, and
        # the frozen receipt CHECK without ``drain``.
        conn = sqlite3.connect(self.db_path)
        conn.execute("PRAGMA foreign_keys = OFF")
        conn.execute("DROP TABLE waits")
        conn.execute("ALTER TABLE jobs DROP COLUMN linkage_config")
        conn.execute("ALTER TABLE receipts RENAME TO receipts_v5_old")
        conn.execute(
            """
            CREATE TABLE receipts (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                job_id INTEGER NOT NULL REFERENCES jobs (id),
                change_id TEXT NOT NULL,
                kind TEXT NOT NULL CHECK (
                    kind IN ('approval','acceptance','reset','pause','steer')
                ),
                checkpoint TEXT NOT NULL,
                material_hash TEXT NOT NULL,
                authority TEXT NOT NULL CHECK (
                    authority IN ('operator','delegated','service')
                ),
                actor_principal TEXT,
                detail TEXT,
                created_at TEXT NOT NULL
            )
            """
        )
        conn.execute("INSERT INTO receipts SELECT * FROM receipts_v5_old")
        conn.execute("DROP TABLE receipts_v5_old")
        conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_receipts_job_change_kind "
            "ON receipts (job_id, change_id, kind)"
        )
        conn.execute("PRAGMA user_version = 5")
        conn.commit()
        conn.close()

        migrated = self.open()
        self.assertEqual(migrated.schema_version(), ledger.CURRENT_SCHEMA_VERSION)
        # Existing records survive.
        self.assertEqual(migrated.get_job(job_id)["id"], job_id)
        self.assertEqual(len(migrated.receipts_for_change(job_id, "ungated")), 1)
        self.assertEqual(migrated.current_policy(job_id)["revision"], 1)
        # The new capabilities are usable immediately.
        receipt_id = migrated.record_stop_request(
            job_id, kind="drain", authority="operator"
        )
        self.assertGreater(receipt_id, 0)
        self.assertEqual(
            [str(row["kind"]) for row in migrated.stop_receipts(job_id)], ["drain"]
        )
        lifecycle.observe_stop_request(migrated, job_id)

    def test_newer_than_code_ledger_is_refused(self) -> None:
        self.register_job(self.ledger)
        self.ledger.close()
        conn = sqlite3.connect(self.db_path)
        conn.execute("PRAGMA user_version = 99")
        conn.commit()
        conn.close()
        before = hashlib.sha256(self.db_path.read_bytes()).digest()
        with self.assertRaises(ledger.LedgerVersionError):
            ledger.open_ledger(self.db_path, repository_root=self.repo)
        after = hashlib.sha256(self.db_path.read_bytes()).digest()
        self.assertEqual(before, after)

    def test_drain_receipt_kind_is_accepted_post_migration(self) -> None:
        job_id = self.register_job(self.ledger)
        self.ledger.record_stop_request(job_id, kind="drain", authority="operator")
        self.assertIn("drain", ledger.RECEIPT_KINDS)


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
