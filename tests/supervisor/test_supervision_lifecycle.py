"""Lifecycle and migration tests for the supervised job surface.

Covers the durable state machine (``register``/``start``/``resume``/``pause``/
``drain``/``cancel``/``complete``), the persisted registration record, the two
stop boundaries and their restart survival, cancellation effects, human waits,
evidence-based completion, the operator endpoint lifecycle verbs, and the
forward-only schema migration that adds the ``waits`` table, the ``drain``
receipt kind, and the registration linkage configuration.
"""

from __future__ import annotations

import argparse
import contextlib
import hashlib
import io
import json
import shutil
import sqlite3
import subprocess
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
        for verb in ("pause", "drain", "resume", "cancel", "approve", "accept"):
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


class FreshReviewRevalidationTests(unittest.TestCase):
    """Failed completion evidence reruns a fresh review for a supervised job."""

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

    def _apply(
        self,
        *,
        supervised: bool,
        verify: tuple = (True, ""),
        checks: tuple = (True, ""),
        clean: tuple = (True, ""),
        round_num: int = 1,
        max_rounds: int = 5,
        with_archive_dir: bool = True,
        recorded_path=None,
        extra_setup=None,
    ) -> tuple:
        module = self.opsx_plan
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        repo = Path(tmp.name)
        archive_rel = "openspec/changes/archive/2026-01-01-change-a"
        if with_archive_dir:
            archived = repo / archive_rel
            archived.mkdir(parents=True)
            (archived / "proposal.md").write_text("# proposal\n", encoding="utf-8")
            (archived / "tasks.md").write_text("- [x] 1.1 done\n", encoding="utf-8")
        if extra_setup is not None:
            extra_setup(repo)
        state = {"changes": {}}
        record = module.state_mod.rec(state, "change-a")
        record["round"] = round_num
        record["max_rounds"] = max_rounds
        record["phase"] = "archive"
        cfg = {"name": "plan"}
        archive_path = archive_rel
        if recorded_path is not None:
            archive_path = (
                recorded_path(repo) if callable(recorded_path) else recorded_path
            )
        payload = {
            "status": "archived",
            "archive_path": archive_path,
            "commit": "",
            "spec_sync_status": "no-delta",
            "summary": "archive succeeded",
        }
        gate = None
        if supervised:
            # A registered supervised job routes post-archive revalidation
            # through the driven bounded-recovery phase; the driver's
            # fresh-review path is what reactivates and requeues the change.
            storage = repo / "service-storage"
            storage.mkdir()
            handle = ledger.open_ledger(
                storage / "supervisor.sqlite3", repository_root=repo
            )
            self.addCleanup(handle.close)
            job_id = handle.register_job(
                run_id="run-1",
                worktree=repo,
                owner="service",
                policy=_policy(),
                operator="operator",
                manifest_content=MANIFEST,
            )
            gate = {"ledger": handle, "job_id": job_id, "policy": _policy()}
        with mock.patch.object(
            module, "verify_direct_archive_done", return_value=verify
        ), mock.patch.object(
            module.groundtruth, "run_fast_checks", return_value=checks
        ), mock.patch.object(
            module.delivery, "verify_post_archive_clean", return_value=clean
        ), mock.patch.object(
            module.state_mod, "pending_manual_tasks", return_value=[]
        ), mock.patch.object(
            module, "_try_notify"
        ):
            action = module.apply_archive_result(
                repo, cfg, state, "change-a", payload, supervised=supervised,
                gate=gate,
            )
            if action == "continue" and record["phase"] == "recovery":
                action = module.drive_supervised_recovery(
                    repo, cfg, state, "change-a", record, gate
                )
        return action, state["changes"]["change-a"], repo

    def test_failed_fast_check_reruns_a_fresh_review_round(self) -> None:
        action, record, repo = self._apply(
            supervised=True, checks=(False, "check failed: smoke")
        )
        self.assertEqual(action, "continue")
        self.assertEqual(record["round"], 2)
        self.assertEqual(record["phase"], "implement")
        self.assertEqual(record["status"], self.opsx_plan.base.PENDING)
        self.assertEqual(record["last_result"], "post_archive_check_failed")
        self.assertEqual(record["archive"]["status"], "failed")
        self.assertIn("post-archive", record["archive"]["reason"])

    def test_requeue_reactivates_the_archived_change(self) -> None:
        """The fresh round can only resolve the change at its active
        location, so requeueing must move the archived artifacts back."""
        action, record, repo = self._apply(
            supervised=True, checks=(False, "check failed: smoke")
        )
        self.assertEqual(action, "continue")
        change_dir = repo / "openspec" / "changes" / "change-a"
        self.assertTrue(change_dir.is_dir())
        self.assertTrue((change_dir / "proposal.md").is_file())
        self.assertTrue((change_dir / "tasks.md").is_file())
        self.assertFalse(
            (repo / "openspec/changes/archive/2026-01-01-change-a").exists()
        )
        reactivated = [
            entry for entry in record["history"]
            if entry.get("status") == "reactivated"
        ]
        self.assertEqual(len(reactivated), 1)
        self.assertEqual(
            reactivated[0]["archive_path"],
            "openspec/changes/archive/2026-01-01-change-a",
        )

    def test_unverified_archive_reruns_a_fresh_review_round(self) -> None:
        action, record, repo = self._apply(
            supervised=True, verify=(False, "no fresh archive worker result recorded")
        )
        self.assertEqual(action, "continue")
        self.assertEqual(record["round"], 2)
        self.assertEqual(record["phase"], "implement")
        self.assertEqual(record["status"], self.opsx_plan.base.PENDING)
        self.assertEqual(record["archive"]["status"], "failed")
        self.assertIn("archive unverified", record["reason"])
        self.assertTrue((repo / "openspec/changes/change-a").is_dir())

    def test_post_archive_dirt_reruns_a_fresh_review_round(self) -> None:
        action, record, repo = self._apply(
            supervised=True, clean=(False, "tracked worktree is dirty")
        )
        self.assertEqual(action, "continue")
        self.assertEqual(record["round"], 2)
        self.assertEqual(record["last_result"], "post_archive_dirty_tracked")
        self.assertEqual(record["status"], self.opsx_plan.base.PENDING)
        self.assertTrue((repo / "openspec/changes/change-a").is_dir())

    def test_requeue_without_recoverable_artifacts_fails_closed(self) -> None:
        """A requeue that cannot restore the change must not pretend the next
        round can resolve it: the failure turns terminal with a named reason."""
        action, record, repo = self._apply(
            supervised=True,
            checks=(False, "check failed: smoke"),
            with_archive_dir=False,
        )
        self.assertEqual(action, "failed")
        self.assertEqual(record["status"], self.opsx_plan.base.FAILED)
        self.assertEqual(record["round"], 1)
        self.assertIn("cannot rerun a fresh review", record["reason"])
        self.assertFalse((repo / "openspec/changes/change-a").exists())

    def test_revalidation_is_bounded_by_the_round_budget(self) -> None:
        action, record, repo = self._apply(
            supervised=True,
            checks=(False, "check failed: smoke"),
            round_num=5,
            max_rounds=5,
        )
        self.assertEqual(action, "failed")
        self.assertEqual(record["status"], self.opsx_plan.base.FAILED)
        self.assertEqual(record["round"], 5)
        self.assertIn("post-archive", record["reason"])

    def test_legacy_run_keeps_terminal_failure(self) -> None:
        action, record, repo = self._apply(
            supervised=False, checks=(False, "check failed: smoke")
        )
        self.assertEqual(action, "stop")
        self.assertEqual(record["status"], self.opsx_plan.base.FAILED)
        self.assertEqual(record["round"], 1)
        self.assertEqual(record["last_result"], "post_archive_check_failed")
        # Legacy runs never touch the archived artifacts.
        self.assertTrue(
            (repo / "openspec/changes/archive/2026-01-01-change-a").is_dir()
        )
        self.assertFalse((repo / "openspec/changes/change-a").exists())

    def test_legacy_unverified_archive_keeps_terminal_failure(self) -> None:
        action, record, repo = self._apply(
            supervised=False, verify=(False, "no fresh archive worker result")
        )
        self.assertEqual(action, "stop")
        self.assertEqual(record["status"], self.opsx_plan.base.FAILED)
        self.assertEqual(record["round"], 1)
        self.assertEqual(record["phase"], "archive")

    def _git(self, repo: Path, *args: str) -> None:
        res = subprocess.run(
            ["git", *args], cwd=repo, capture_output=True, text=True
        )
        self.assertEqual(res.returncode, 0, res.stderr)

    def test_reactivated_change_completes_a_fresh_archive_round(self) -> None:
        """Loop-level regression: archive a real change, fail a post-archive
        check, then prove the reactivated artifacts are consumed by the real
        implement/review/archive loop — each fresh stage dispatch applies its
        result through the same control-flow functions the run engine uses —
        and the fresh round completes against verified archive evidence."""
        module = self.opsx_plan
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        repo = Path(tmp.name)
        self._git(repo, "init")
        (repo / ".gitignore").write_text(
            "openspec/changes/archive/\n", encoding="utf-8"
        )
        (repo / "tracked.txt").write_text("base\n", encoding="utf-8")
        self._git(repo, "add", ".")
        self._git(
            repo, "-c", "user.email=test@example.invalid",
            "-c", "user.name=Test User", "commit", "-m", "init",
        )
        cid = "change-a"
        change_dir = repo / "openspec" / "changes" / cid
        change_dir.mkdir(parents=True)
        (change_dir / "proposal.md").write_text("# proposal\n", encoding="utf-8")
        (change_dir / "tasks.md").write_text("- [x] 1.1 done\n", encoding="utf-8")
        cfg = {
            "name": "plan",
            "check_timeout_minutes": 1,
            "fast_checks": [],
            "no_progress_limit": 3,
        }
        state = {"changes": {}}
        record = module.state_mod.rec(state, cid)
        record["round"] = 1
        record["max_rounds"] = 5
        record["phase"] = "archive"
        # A registered supervised job: post-archive revalidation routes
        # through the driven bounded-recovery phase.
        storage_tmp = tempfile.TemporaryDirectory()
        self.addCleanup(storage_tmp.cleanup)
        handle = ledger.open_ledger(
            Path(storage_tmp.name) / "supervisor.sqlite3", repository_root=repo
        )
        self.addCleanup(handle.close)
        job_id = handle.register_job(
            run_id="run-1",
            worktree=repo,
            owner="service",
            policy=_policy(),
            operator="operator",
            manifest_content=MANIFEST,
        )
        gate = {"ledger": handle, "job_id": job_id, "policy": _policy()}

        def run_archive_stage(date: str, checks: tuple | None = None) -> str:
            """Drive one archive stage dispatch: the archive worker moves the
            change into a dated archive directory (what `openspec archive`
            does) and reports the evidence; the controller then applies the
            result through the same function the run engine's stage dispatch
            calls, with real archive verification against the repo. When the
            outcome routes to bounded recovery, the recovery phase is driven
            exactly as the run loop drives it."""
            archive_rel = f"openspec/changes/archive/{date}-{cid}"
            dst = repo / archive_rel
            dst.parent.mkdir(parents=True, exist_ok=True)
            shutil.move(str(change_dir), str(dst))
            payload = {
                "status": "archived",
                "archive_path": archive_rel,
                "commit": "",
                "spec_sync_status": "no-delta",
                "summary": "archive succeeded",
            }

            def _apply_and_drive(checks_mock) -> str:
                with checks_mock, mock.patch.object(module, "_try_notify"):
                    action = module.apply_archive_result(
                        repo, cfg, state, cid, payload, supervised=True, gate=gate
                    )
                    if action == "continue" and record["phase"] == "recovery":
                        action = module.drive_supervised_recovery(
                            repo, cfg, state, cid, record, gate
                        )
                    return action

            if checks is None:
                # Real fast checks: cfg declares none, so the gate passes on
                # its own evidence.
                return _apply_and_drive(contextlib.nullcontext())
            return _apply_and_drive(
                mock.patch.object(
                    module.groundtruth, "run_fast_checks", return_value=checks
                )
            )
            with mock.patch.object(
                module.groundtruth, "run_fast_checks", return_value=checks
            ), mock.patch.object(module, "_try_notify"):
                return module.apply_archive_result(
                    repo, cfg, state, cid, payload, supervised=True
                )

        # Round 1, archive stage: the archive evidence is real and verified
        # against the repo, but the post-archive fast check fails. The loop
        # must reactivate the change at its active location and requeue a
        # fresh round rather than treating the prior archive as done.
        action = run_archive_stage(
            "2026-01-01", checks=(False, "check failed: smoke")
        )
        self.assertEqual(action, "continue")
        self.assertEqual(record["round"], 2)
        self.assertEqual(record["phase"], "implement")
        self.assertEqual(record["last_result"], "post_archive_check_failed")
        self.assertTrue(change_dir.is_dir())
        self.assertTrue((change_dir / "proposal.md").is_file())
        self.assertTrue((change_dir / "tasks.md").is_file())
        self.assertFalse(
            (repo / "openspec/changes/archive/2026-01-01-change-a").exists()
        )

        # Round 2, fresh implement stage dispatch: the result is applied
        # against the reactivated artifacts — the completeness gate parses
        # the restored tasks.md at the active location and advances the
        # change to review.
        with mock.patch.object(module, "_try_notify"):
            action = module.apply_implement_result(
                repo, cfg, state, cid,
                {
                    "status": "implemented",
                    "summary": "fresh round implementation complete",
                    "progress_made": True,
                    "task_counts": {"complete": 1, "total": 1},
                    "completed_tasks": ["1.1 done"],
                    "remaining_tasks": [],
                    "files_touched": [f"openspec/changes/{cid}/proposal.md"],
                },
            )
        self.assertEqual(action, "continue")
        self.assertEqual(record["phase"], "review")
        self.assertEqual(record["last_result"], "implement_completed")

        # Round 2, fresh review stage dispatch: a clean pass advances the
        # change to archive.
        with mock.patch.object(module, "_try_notify"):
            action = module.apply_review_result(
                repo, cfg, state, cid,
                {
                    "status": "reviewed",
                    "verdict": "pass",
                    "summary": "fresh review passed",
                    "finding_counts": {"critical": 0, "warning": 0, "note": 0},
                    "findings": [],
                    "fix_prompt": "",
                },
            )
        self.assertEqual(action, "continue")
        self.assertEqual(record["phase"], "archive")
        self.assertEqual(record["last_result"], "review_passed")

        # Round 2, fresh archive stage dispatch: the worker archives the
        # reactivated change again with fresh dated evidence; real archive
        # verification, real fast checks, and the real post-archive
        # cleanliness gate all pass, so the loop completes the change.
        action = run_archive_stage("2026-01-02")
        self.assertEqual(action, "done")
        self.assertEqual(record["phase"], "done")
        self.assertEqual(record["status"], module.base.DONE)
        self.assertEqual(record["archive"]["status"], "passed")
        self.assertEqual(
            record["archive"]["path"],
            "openspec/changes/archive/2026-01-02-change-a",
        )
        self.assertFalse(change_dir.exists())
        self.assertTrue(
            (repo / "openspec/changes/archive/2026-01-02-change-a").is_dir()
        )
        self.assertTrue(
            (repo / "openspec/changes/archive/2026-01-02-change-a/tasks.md").is_file()
        )
        history = [
            (entry.get("round"), entry.get("phase"), entry.get("status"))
            for entry in record["history"]
        ]
        self.assertIn((1, "archive", "archived"), history)
        self.assertIn((1, "archive", "reactivated"), history)
        self.assertIn((2, "implement", "implemented"), history)
        self.assertIn((2, "review", "pass"), history)
        self.assertIn((2, "archive", "archived"), history)

    def test_hostile_absolute_archive_path_fails_closed(self) -> None:
        """An absolute recorded path — even one pointing at the real
        canonical archive directory — is rejected before any move."""
        action, record, repo = self._apply(
            supervised=True,
            checks=(False, "check failed: smoke"),
            recorded_path=lambda repo: str(
                repo / "openspec/changes/archive/2026-01-01-change-a"
            ),
        )
        self.assertEqual(action, "failed")
        self.assertEqual(record["status"], self.opsx_plan.base.FAILED)
        self.assertEqual(record["round"], 1)
        self.assertIn("cannot rerun a fresh review", record["reason"])
        self.assertIn("refusing reactivation", record["reason"])
        # Nothing was moved: the archive directory is intact and the change
        # was not reactivated.
        self.assertTrue(
            (repo / "openspec/changes/archive/2026-01-01-change-a/proposal.md").is_file()
        )
        self.assertFalse((repo / "openspec/changes/change-a").exists())

    def test_hostile_traversal_archive_path_fails_closed(self) -> None:
        """A recorded path that traverses out of the archive root into
        another change's active directory is rejected before any move."""

        def extra_setup(repo: Path) -> None:
            victim = repo / "openspec" / "changes" / "change-b"
            victim.mkdir(parents=True)
            (victim / "proposal.md").write_text("# victim\n", encoding="utf-8")

        action, record, repo = self._apply(
            supervised=True,
            checks=(False, "check failed: smoke"),
            recorded_path="openspec/changes/archive/../change-b",
            extra_setup=extra_setup,
        )
        self.assertEqual(action, "failed")
        self.assertEqual(record["status"], self.opsx_plan.base.FAILED)
        self.assertIn("refusing reactivation", record["reason"])
        self.assertTrue(
            (repo / "openspec/changes/change-b/proposal.md").is_file()
        )
        self.assertTrue(
            (repo / "openspec/changes/archive/2026-01-01-change-a").is_dir()
        )
        self.assertFalse((repo / "openspec/changes/change-a").exists())

    def test_hostile_mismatched_archive_path_fails_closed(self) -> None:
        """A recorded path naming a dated archive directory for a different
        change is rejected before any move."""

        def extra_setup(repo: Path) -> None:
            other = repo / "openspec/changes/archive/2026-01-01-change-b"
            other.mkdir(parents=True)
            (other / "proposal.md").write_text("# other\n", encoding="utf-8")

        action, record, repo = self._apply(
            supervised=True,
            checks=(False, "check failed: smoke"),
            recorded_path="openspec/changes/archive/2026-01-01-change-b",
            extra_setup=extra_setup,
        )
        self.assertEqual(action, "failed")
        self.assertEqual(record["status"], self.opsx_plan.base.FAILED)
        self.assertIn("refusing reactivation", record["reason"])
        self.assertTrue(
            (repo / "openspec/changes/archive/2026-01-01-change-b/proposal.md").is_file()
        )
        self.assertTrue(
            (repo / "openspec/changes/archive/2026-01-01-change-a").is_dir()
        )
        self.assertFalse((repo / "openspec/changes/change-a").exists())

    def test_hostile_symlink_escaping_archive_root_fails_closed(self) -> None:
        """A canonical-looking archive entry that symlinks outside the
        archive root is rejected before any move."""

        def extra_setup(repo: Path) -> None:
            outside = repo / "outside"
            outside.mkdir()
            (outside / "proposal.md").write_text("# outside\n", encoding="utf-8")
            link = repo / "openspec/changes/archive/2026-01-01-change-a"
            link.parent.mkdir(parents=True, exist_ok=True)
            link.symlink_to(outside, target_is_directory=True)

        action, record, repo = self._apply(
            supervised=True,
            checks=(False, "check failed: smoke"),
            with_archive_dir=False,
            extra_setup=extra_setup,
        )
        self.assertEqual(action, "failed")
        self.assertEqual(record["status"], self.opsx_plan.base.FAILED)
        self.assertIn("refusing reactivation", record["reason"])
        self.assertTrue((repo / "outside/proposal.md").is_file())
        self.assertFalse((repo / "openspec/changes/change-a").exists())

    def test_symlinked_active_path_fails_closed(self) -> None:
        """A symlink at the canonical active path must not satisfy the
        'already active' no-op check — ``exists()`` follows the link, so
        without the real-directory guard reactivation would requeue the
        fresh loop straight into unrelated artifacts. Reactivation fails
        closed: the archive stays untouched and no fresh round is
        dispatched."""

        def extra_setup(repo: Path) -> None:
            outside = repo / "unrelated"
            outside.mkdir()
            (outside / "proposal.md").write_text("# unrelated\n", encoding="utf-8")
            link = repo / "openspec" / "changes" / "change-a"
            link.parent.mkdir(parents=True, exist_ok=True)
            link.symlink_to(outside, target_is_directory=True)

        action, record, repo = self._apply(
            supervised=True,
            checks=(False, "check failed: smoke"),
            extra_setup=extra_setup,
        )
        self.assertEqual(action, "failed")
        self.assertEqual(record["status"], self.opsx_plan.base.FAILED)
        self.assertEqual(record["round"], 1)
        self.assertEqual(record["phase"], "recovery")
        self.assertIn("cannot rerun a fresh review", record["reason"])
        self.assertIn("refusing reactivation", record["reason"])
        # The archive remains untouched — nothing was restored or moved.
        self.assertTrue(
            (repo / "openspec/changes/archive/2026-01-01-change-a/proposal.md").is_file()
        )
        # The symlink was not followed or clobbered: it still points at the
        # unrelated directory and no change artifacts appeared through it.
        active = repo / "openspec/changes/change-a"
        self.assertTrue(active.is_symlink())
        self.assertEqual(active.readlink(), repo / "unrelated")
        self.assertFalse((active / "tasks.md").exists())
        self.assertEqual(
            (repo / "unrelated/proposal.md").read_text(encoding="utf-8"),
            "# unrelated\n",
        )

    def test_dangling_link_at_active_path_fails_closed(self) -> None:
        """A dangling symlink at the canonical active path fails ``exists()``
        but must still fail closed rather than being clobbered by the
        restored archive or treated as absent-and-safe."""

        def extra_setup(repo: Path) -> None:
            link = repo / "openspec" / "changes" / "change-a"
            link.parent.mkdir(parents=True, exist_ok=True)
            link.symlink_to(repo / "gone", target_is_directory=True)

        action, record, repo = self._apply(
            supervised=True,
            checks=(False, "check failed: smoke"),
            extra_setup=extra_setup,
        )
        self.assertEqual(action, "failed")
        self.assertEqual(record["status"], self.opsx_plan.base.FAILED)
        self.assertEqual(record["round"], 1)
        self.assertIn("refusing reactivation", record["reason"])
        # The archive remains untouched and the dangling link survives.
        self.assertTrue(
            (repo / "openspec/changes/archive/2026-01-01-change-a/proposal.md").is_file()
        )
        self.assertTrue((repo / "openspec/changes/change-a").is_symlink())

    def test_plain_file_at_active_path_fails_closed(self) -> None:
        """A plain file occupying the canonical active path is not the
        active change: reactivation fails closed instead of moving the
        archive over it."""

        def extra_setup(repo: Path) -> None:
            blocker = repo / "openspec" / "changes" / "change-a"
            blocker.parent.mkdir(parents=True, exist_ok=True)
            blocker.write_text("not a change\n", encoding="utf-8")

        action, record, repo = self._apply(
            supervised=True,
            checks=(False, "check failed: smoke"),
            extra_setup=extra_setup,
        )
        self.assertEqual(action, "failed")
        self.assertEqual(record["status"], self.opsx_plan.base.FAILED)
        self.assertEqual(record["round"], 1)
        self.assertIn("refusing reactivation", record["reason"])
        self.assertTrue(
            (repo / "openspec/changes/archive/2026-01-01-change-a/proposal.md").is_file()
        )
        self.assertEqual(
            (repo / "openspec/changes/change-a").read_text(encoding="utf-8"),
            "not a change\n",
        )

    def test_suffix_matching_sibling_is_not_the_active_change(self) -> None:
        """A sibling directory whose name merely ends in ``-<cid>`` must
        never count as the active change: reactivation still restores the
        canonical archive at the exact active path — the sibling is neither
        accepted as active nor receives the restored artifacts a fresh
        dispatch would act on."""
        def extra_setup(repo: Path) -> None:
            sibling = repo / "openspec" / "changes" / "unrelated-change-a"
            sibling.mkdir(parents=True)
            (sibling / "proposal.md").write_text("# unrelated\n", encoding="utf-8")

        action, record, repo = self._apply(
            supervised=True,
            checks=(False, "check failed: smoke"),
            extra_setup=extra_setup,
        )
        self.assertEqual(action, "continue")
        # The canonical archive was moved to the exact active path.
        active = repo / "openspec" / "changes" / "change-a"
        self.assertTrue(active.is_dir())
        self.assertEqual(
            (active / "proposal.md").read_text(encoding="utf-8"), "# proposal\n"
        )
        self.assertTrue((active / "tasks.md").is_file())
        self.assertFalse(
            (repo / "openspec/changes/archive/2026-01-01-change-a").exists()
        )
        # The unrelated sibling was left completely untouched.
        sibling = repo / "openspec" / "changes" / "unrelated-change-a"
        self.assertEqual(
            (sibling / "proposal.md").read_text(encoding="utf-8"), "# unrelated\n"
        )

    def test_verify_archive_ignores_suffix_matching_sibling(self) -> None:
        """Archive verification checks the exact canonical active path, so a
        suffix-matching sibling left behind by unrelated work does not read
        as 'the change is still active' and spuriously fail verification."""
        module = self.opsx_plan
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        repo = Path(tmp.name)
        self._git(repo, "init")
        (repo / ".gitignore").write_text(
            "openspec/changes/archive/\n", encoding="utf-8"
        )
        (repo / "tracked.txt").write_text("base\n", encoding="utf-8")
        self._git(repo, "add", ".")
        self._git(
            repo, "-c", "user.email=test@example.invalid",
            "-c", "user.name=Test User", "commit", "-m", "init",
        )
        cid = "change-a"
        archive_rel = f"openspec/changes/archive/2026-01-01-{cid}"
        archived = repo / archive_rel
        archived.mkdir(parents=True)
        (archived / "proposal.md").write_text("# proposal\n", encoding="utf-8")
        (archived / "tasks.md").write_text("- [x] 1.1 done\n", encoding="utf-8")
        sibling = repo / "openspec" / "changes" / f"unrelated-{cid}"
        sibling.mkdir(parents=True)
        (sibling / "proposal.md").write_text("# unrelated\n", encoding="utf-8")
        state = {"changes": {}}
        record = module.state_mod.rec(state, cid)
        record["archive"].update(
            {"status": "passed", "path": archive_rel, "commit": "", "reason": ""}
        )
        ok, why = module.verify_direct_archive_done(repo, cid, record)
        self.assertTrue(ok, why)

    def test_verify_archive_fails_on_symlink_at_active_path(self) -> None:
        """A symlink — even a dangling one — at the canonical active path
        counts as 'still exists', so verification fails and routes to
        reactivation, which itself fails closed on non-directories."""
        module = self.opsx_plan
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        repo = Path(tmp.name)
        self._git(repo, "init")
        (repo / ".gitignore").write_text(
            "openspec/changes/archive/\n", encoding="utf-8"
        )
        (repo / "tracked.txt").write_text("base\n", encoding="utf-8")
        self._git(repo, "add", ".")
        self._git(
            repo, "-c", "user.email=test@example.invalid",
            "-c", "user.name=Test User", "commit", "-m", "init",
        )
        cid = "change-a"
        archive_rel = f"openspec/changes/archive/2026-01-01-{cid}"
        archived = repo / archive_rel
        archived.mkdir(parents=True)
        (archived / "proposal.md").write_text("# proposal\n", encoding="utf-8")
        (archived / "tasks.md").write_text("- [x] 1.1 done\n", encoding="utf-8")
        link = repo / "openspec" / "changes" / cid
        link.symlink_to(repo / "gone", target_is_directory=True)
        state = {"changes": {}}
        record = module.state_mod.rec(state, cid)
        record["archive"].update(
            {"status": "passed", "path": archive_rel, "commit": "", "reason": ""}
        )
        ok, why = module.verify_direct_archive_done(repo, cid, record)
        self.assertFalse(ok)
        self.assertIn("still exists", why)


class CliLifecycleCommandTests(LifecycleTestCase):
    """Each lifecycle command end to end through the cmd_supervise handlers."""

    def setUp(self) -> None:
        super().setUp()
        from lib.orchestrator import cmd_supervise

        self.cli = cmd_supervise
        (self.repo / "plan.toml").write_text(
            '[plan]\nname = "cli-plan"\nadapter = "opencode"\n'
            "\n[[changes]]\nid = \"change-a\"\npause_before = false\ndepends_on = []\n",
            encoding="utf-8",
        )

    def _args(self, **overrides: object) -> argparse.Namespace:
        ns = argparse.Namespace(
            repo=str(self.repo),
            plan="plan.toml",
            store=str(self.db_path),
            job_id=None,
            json=False,
            no_drive=True,
            primary_session=False,
            budget_usd=0.0,
            budget_minutes=None,
            per_action_usd=None,
            per_action_minutes=None,
            deadline_minutes=None,
            max_incident_attempts=None,
        )
        for key, value in overrides.items():
            setattr(ns, key, value)
        return ns

    def _register(self) -> int:
        with mock.patch.object(
            authority, "require_authority_backend", return_value=mock.Mock()
        ):
            rc = self.cli.cmd_supervise_register(self._args())
        self.assertEqual(rc, 0)
        job = lifecycle.job_for_worktree(
            self.ledger, self.repo, repository_root=self.repo
        )
        return int(job["id"])

    def _start(self) -> int:
        rc = self.cli.cmd_supervise_start(self._args(no_drive=True))
        self.assertEqual(rc, 0)
        job = lifecycle.job_for_worktree(
            self.ledger, self.repo, repository_root=self.repo
        )
        self.assertEqual(job["state"], "active")
        return int(job["id"])

    def _mute_endpoint(self):
        return mock.patch.object(
            self.cli, "_operator_socket_configured", return_value=False
        )

    def test_register_records_the_full_job(self) -> None:
        job_id = self._register()
        job = self.ledger.get_job(job_id)
        self.assertEqual(job["state"], "registered")
        # The ledger stores the worktree relative to the repository root.
        self.assertEqual(
            (self.repo / job["worktree_path"]).resolve(), self.repo
        )
        policy = self.ledger.current_policy(job_id)
        self.assertEqual(policy["revision"], 1)
        self.assertTrue(policy["manifest_snapshot_hash"])
        self.assertEqual(
            self.ledger.job_linkage_config(job_id)["adapter"], "opencode"
        )

    def test_register_fails_closed_on_an_unsupported_host(self) -> None:
        stderr = io.StringIO()
        with mock.patch.object(
            authority,
            "require_authority_backend",
            side_effect=authority.UnsupportedHostError("no backend"),
        ), contextlib.redirect_stderr(stderr):
            rc = self.cli.cmd_supervise_register(self._args())
        self.assertEqual(rc, 1)
        self.assertIn("UnsupportedHostError", stderr.getvalue())
        with self.assertRaises(lifecycle.UnknownJobError):
            lifecycle.job_for_worktree(
                self.ledger, self.repo, repository_root=self.repo
            )

    def test_start_then_inspect_projects_the_active_job(self) -> None:
        job_id = self._register()
        self.assertEqual(self._start(), job_id)
        stdout = io.StringIO()
        with contextlib.redirect_stdout(stdout):
            rc = self.cli.cmd_supervise_inspect(self._args(json=True))
        self.assertEqual(rc, 0)
        projection = json.loads(stdout.getvalue())
        self.assertEqual(projection["job_id"], job_id)
        self.assertEqual(projection["state"], "active")
        self.assertEqual(projection["policy"]["revision"], 1)
        self.assertEqual(projection["waits"], [])

    def test_pause_resume_drain_and_cancel_drive_the_state_machine(self) -> None:
        self._register()
        job_id = self._start()
        with self._mute_endpoint():
            self.assertEqual(self.cli.cmd_supervise_pause(self._args()), 0)
            self.assertEqual(self.ledger.get_job(job_id)["state"], "paused")
            self.assertEqual(self.cli.cmd_supervise_resume(self._args()), 0)
            self.assertEqual(self.ledger.get_job(job_id)["state"], "active")
            self.assertEqual(self.cli.cmd_supervise_drain(self._args()), 0)
            self.assertEqual(self.ledger.get_job(job_id)["state"], "paused")
            self.assertEqual(self.cli.cmd_supervise_resume(self._args()), 0)
            self.assertEqual(self.cli.cmd_supervise_cancel(self._args()), 0)
            self.assertEqual(self.ledger.get_job(job_id)["state"], "cancelled")

    def test_mutations_fail_closed_when_the_broker_is_unreachable(self) -> None:
        self._register()
        job_id = self._start()
        stderr = io.StringIO()
        with mock.patch.object(
            self.cli, "_operator_socket_configured", return_value=True
        ), mock.patch.object(
            self.cli.supervision_mod,
            "call_operator",
            side_effect=broker_mod.BrokerUnavailableError("endpoint unreachable"),
        ), contextlib.redirect_stderr(stderr):
            rc = self.cli.cmd_supervise_pause(self._args())
        self.assertEqual(rc, 1)
        self.assertIn("BrokerUnavailableError", stderr.getvalue())
        self.assertEqual(self.ledger.get_job(job_id)["state"], "active")

    def test_unregistered_worktree_is_a_named_unknown_job_error(self) -> None:
        stderr = io.StringIO()
        with self._mute_endpoint(), contextlib.redirect_stderr(stderr):
            rc = self.cli.cmd_supervise_pause(self._args())
        self.assertEqual(rc, 1)
        self.assertIn("UnknownJobError", stderr.getvalue())

    def test_illegal_transition_and_terminal_job_are_named_errors(self) -> None:
        self._register()
        job_id = self._start()
        stderr = io.StringIO()
        with contextlib.redirect_stderr(stderr):
            rc = self.cli.cmd_supervise_start(self._args(no_drive=True))
        self.assertEqual(rc, 1)
        self.assertIn("IllegalTransitionError", stderr.getvalue())
        with self._mute_endpoint():
            self.assertEqual(self.cli.cmd_supervise_cancel(self._args()), 0)
        stderr = io.StringIO()
        with self._mute_endpoint(), contextlib.redirect_stderr(stderr):
            rc = self.cli.cmd_supervise_pause(self._args())
        self.assertEqual(rc, 1)
        self.assertIn("TerminalJobError", stderr.getvalue())
        self.assertEqual(self.ledger.get_job(job_id)["state"], "cancelled")


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
        conn.execute(
            """
            INSERT INTO receipts (
                id, job_id, change_id, kind, checkpoint, material_hash,
                authority, actor_principal, detail, created_at
            )
            SELECT id, job_id, change_id, kind, checkpoint, material_hash,
                   authority, actor_principal, detail, created_at
            FROM receipts_v5_old
            """
        )
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
