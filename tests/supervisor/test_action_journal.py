"""Contract tests for the journal-integrated supervised dispatch boundary.

Covers the engine integration of ``lib/orchestrator/journal_dispatch.py``:
intent durability before a side effect, inner-stage journaling through the
real run engine, the four pre-dispatch gates, per-action plan/policy
freshness, uncertainty and evidence reconciliation, deduplicating
re-observant replay, both worker dispatch paths, and the untouched legacy
path.
"""

from __future__ import annotations

import importlib.util
import json
import os
import subprocess
import sys
import tempfile
import unittest
from contextlib import contextmanager
from pathlib import Path
from unittest import mock

from lib.orchestrator import journal_dispatch
from lib.supervisor import broker as broker_mod
from lib.supervisor import budgets as budget_mod
from lib.supervisor import endpoints as endpoints_mod
from lib.supervisor import ledger, lock as lock_mod, model_policy

SCRIPT = Path(__file__).resolve().parents[2] / "orchestrator" / "opsx-plan.py"


def load_opsx_plan():
    spec = importlib.util.spec_from_file_location("opsx_plan", SCRIPT)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules["opsx_plan"] = module
    spec.loader.exec_module(module)
    return module


def git(repo: Path, *args: str) -> None:
    subprocess.run(["git", *args], cwd=repo, check=True, capture_output=True, text=True)


def _selection() -> dict:
    return {
        "version": model_policy.MODEL_POLICY_VERSION,
        "roles": {
            "supervisor": "openai/gpt-4o",
            "supervised_author": "openai/gpt-4o",
            "implementer": "openai/gpt-4o",
            "reviewer": "openai/gpt-4o",
            "archiver": "openai/gpt-4o",
            "acceptance_reviewer": "openai/gpt-4o",
            "fixer": "openai/gpt-4o",
            "verifier": "openai/gpt-4o",
            "implementer_escalation": "openai/gpt-4o",
        },
        "stages": {
            "create": "supervised_author",
            "implement": "implementer",
            "review": "reviewer",
            "archive": "archiver",
            "acceptance": "acceptance_reviewer",
            "fix": "fixer",
            "verify": "verifier",
            "escalate": "implementer_escalation",
        },
    }


def _policy(
    *, total_cost_usd: float = 1000.0, per_action_cost_usd: float | None = None,
    allowlist: list[str] | None = None, roles: dict | None = None,
) -> dict:
    selection = _selection()
    if roles is not None:
        selection["roles"] = roles
    return {
        "authority_config": {"mode": "policy-bound"},
        "model_selection": selection,
        "inexpensive_allowlist": {
            "version": model_policy.MODEL_POLICY_VERSION,
            "models": allowlist if allowlist is not None else ["openai/gpt-4o"],
            "source": "test fixture",
        },
        "manifest_snapshot_hash": "deadbeef",
        "budgets": {
            "version": budget_mod.BUDGET_SCHEMA_VERSION,
            "total_cost_usd": total_cost_usd,
            "per_action_cost_usd": per_action_cost_usd,
            "total_elapsed_minutes": None,
            "per_action_elapsed_minutes": None,
            "max_incident_attempts": None,
        },
        "deadlines": {
            "version": budget_mod.BUDGET_SCHEMA_VERSION,
            "execution_deadline_minutes": None,
        },
    }


MANIFEST = "[[changes]]\nid = \"add-journal-test\"\npause_before = false\ndepends_on = []\n"


class ActionJournalTestCase(unittest.TestCase):
    def setUp(self) -> None:
        self.opsx = load_opsx_plan()
        journal_dispatch.end_active_dispatch()
        self.addCleanup(journal_dispatch.end_active_dispatch)
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.repo = self.root / "repo"
        self.repo.mkdir()
        git(self.repo, "init")
        (self.repo / "tracked.txt").write_text("base\n", encoding="utf-8")
        git(self.repo, "add", "tracked.txt")
        git(
            self.repo, "-c", "user.email=t@e.invalid", "-c", "user.name=T",
            "commit", "-m", "init",
        )
        self.storage = self.root / "service-storage"
        self.storage.mkdir()
        self.db_path = self.storage / "supervisor.sqlite3"
        self.ledger = ledger.open_ledger(self.db_path, repository_root=self.repo)
        self.addCleanup(self.ledger.close)
        self.cid = "add-journal-test"
        self.manifest_path = self.repo / "plan.toml"
        self.manifest_path.write_text(MANIFEST, encoding="utf-8")
        patcher = mock.patch.dict(
            os.environ, {"OPSX_SUPERVISOR_STATE_FILE": str(self.db_path)}
        )
        patcher.start()
        self.addCleanup(patcher.stop)

    def register(
        self, *, policy: dict | None = None, manifest_content: str = MANIFEST
    ) -> int:
        self.principal = "opsx-service"
        return self.ledger.register_job(
            run_id="run-1",
            worktree=self.repo,
            owner="service",
            owner_principal=self.principal,
            policy=policy if policy is not None else _policy(),
            operator="operator",
            manifest_content=manifest_content,
        )

    def gate(self, job_id: int, policy: dict | None = None) -> dict:
        current = policy if policy is not None else self.ledger.current_policy(job_id)
        return {
            "ledger": self.ledger,
            "job_id": job_id,
            "policy": current,
            "policy_revision": int(current["revision"]),
            "manifest_snapshot_hash": current["manifest_snapshot_hash"],
            "manifest_path": str(self.manifest_path),
        }

    @contextmanager
    def fenced(self, job_id: int):
        identity = lock_mod.current_identity()
        self.ledger.record_fencing(
            job_id, event="acquired", owner="test-service",
            pid=identity["pid"], process_start=identity["process_start"],
            boot_id=identity["boot_id"], host=identity["host"],
        )
        try:
            yield
        finally:
            self.ledger.record_fencing(
                job_id, event="released", owner="test-service",
                pid=identity["pid"], process_start=identity["process_start"],
                boot_id=identity["boot_id"], host=identity["host"],
            )

    def dispatch(self, job_id: int, *, stage: str = "implement", policy: dict | None = None) -> dict:
        r = {"escalation": {"active": False}}
        with self.fenced(job_id):
            return journal_dispatch.gated_dispatch(
                self.repo, {"changes": {self.cid: {"timeout_minutes": 1}}},
                self.gate(job_id, policy), self.cid, stage, 1, r, "run-1",
                resolved_model="openai/gpt-4o",
            )


class IntentDurabilityTests(ActionJournalTestCase):
    def test_intent_survives_interruption_before_spawn(self) -> None:
        job_id = self.register()
        gate = self.gate(job_id)
        with self.fenced(job_id):
            with mock.patch.object(
                journal_dispatch.budget_mod, "reserve",
                side_effect=KeyboardInterrupt("interrupted before spawn"),
            ):
                with self.assertRaises(KeyboardInterrupt):
                    journal_dispatch.gated_dispatch(
                        self.repo,
                        {"changes": {self.cid: {"timeout_minutes": 1}}},
                        gate, self.cid, "implement", 1,
                        {"escalation": {"active": False}}, "run-1",
                        resolved_model="openai/gpt-4o",
                    )
        # The intent committed before the simulated interruption is durable.
        actions = self.ledger.list_actions(job_id)
        self.assertEqual(len(actions), 1)
        self.assertEqual(actions[0]["state"], "intent")
        self.assertEqual(actions[0]["kind"], "implement")
        self.assertEqual(self.ledger.list_dispatches(actions[0]["id"]), [])


class FourGateTests(ActionJournalTestCase):
    def _no_side_effects(self, job_id: int) -> None:
        self.assertEqual(self.ledger.list_actions(job_id), [])
        self.assertEqual(self.ledger.reservations_for_job(job_id), [])

    def test_lock_not_held_blocks_before_side_effects(self) -> None:
        job_id = self.register()
        with self.assertRaises(journal_dispatch.LockGateError) as ctx:
            journal_dispatch.gated_dispatch(
                self.repo, {"changes": {self.cid: {"timeout_minutes": 1}}},
                self.gate(job_id), self.cid, "implement", 1,
                {"escalation": {"active": False}}, "run-1",
            )
        self.assertEqual(ctx.exception.gate, journal_dispatch.GATE_LOCK)
        self._no_side_effects(job_id)

    def test_authority_refusal_blocks_before_side_effects(self) -> None:
        job_id = self.register()
        with self.fenced(job_id):
            with mock.patch.object(
                journal_dispatch.broker_mod, "assert_resume_clear",
                side_effect=broker_mod.BrokerMediationError("authority refused"),
            ):
                with self.assertRaises(journal_dispatch.AuthorityGateError) as ctx:
                    journal_dispatch.gated_dispatch(
                        self.repo, {"changes": {self.cid: {"timeout_minutes": 1}}},
                        self.gate(job_id), self.cid, "implement", 1,
                        {"escalation": {"active": False}}, "run-1",
                    )
        self.assertEqual(ctx.exception.gate, journal_dispatch.GATE_AUTHORITY)
        self._no_side_effects(job_id)

    def test_revoked_approval_state_blocks_each_action(self) -> None:
        manifest = MANIFEST.replace("pause_before = false", "pause_before = true")
        self.manifest_path.write_text(manifest, encoding="utf-8")
        job_id = self.register(manifest_content=manifest)
        with self.fenced(job_id):
            with self.assertRaises(journal_dispatch.AuthorityGateError) as ctx:
                journal_dispatch.gated_dispatch(
                    self.repo, {"changes": {self.cid: {"timeout_minutes": 1}}},
                    self.gate(job_id), self.cid, "implement", 1,
                    {"escalation": {"active": False}}, "run-1",
                    resolved_model="openai/gpt-4o",
                )
        self.assertIn("not dispatchable", str(ctx.exception))
        self._no_side_effects(job_id)

    def test_model_policy_missing_identity_blocks(self) -> None:
        roles = _selection()["roles"]
        roles.pop("implementer")
        with self.assertRaises(journal_dispatch.ModelPolicyGateError) as ctx:
            journal_dispatch.assert_model_policy_gate(
                _policy(roles=roles), "implementer"
            )
        self.assertEqual(ctx.exception.gate, journal_dispatch.GATE_MODEL_POLICY)

    def test_model_policy_unallowlisted_blocks(self) -> None:
        with self.assertRaises(journal_dispatch.ModelPolicyGateError) as ctx:
            journal_dispatch.assert_model_policy_gate(
                _policy(allowlist=["other/model"]), "implementer",
                resolved_model="openai/gpt-4o",
            )
        self.assertIn("allowlist", str(ctx.exception))

    def test_model_policy_mismatched_identity_blocks(self) -> None:
        with self.assertRaises(journal_dispatch.ModelPolicyGateError) as ctx:
            journal_dispatch.assert_model_policy_gate(
                _policy(allowlist=["openai/gpt-4o", "openai/other-model"]),
                "implementer",
                resolved_model="openai/other-model",
            )
        self.assertIn("differs", str(ctx.exception))

    def test_escalated_implement_is_journaled_under_escalation_role(self) -> None:
        job_id = self.register()
        with self.fenced(job_id):
            entry = journal_dispatch.gated_dispatch(
                self.repo, {"changes": {self.cid: {"timeout_minutes": 1}}},
                self.gate(job_id), self.cid, "implement", 2,
                {"escalation": {"active": True}}, "run-1",
                resolved_model="openai/gpt-4o",
            )
        self.assertEqual(entry["role"], "implementer_escalation")
        self.assertEqual(
            self.ledger.get_action(entry["action_id"])["kind"], "implement"
        )

    def test_budget_exhaustion_blocks_at_the_same_boundary(self) -> None:
        job_id = self.register(
            policy=_policy(total_cost_usd=1.0, per_action_cost_usd=1000.0)
        )
        with self.fenced(job_id):
            with self.assertRaises(journal_dispatch.BudgetGateError) as ctx:
                journal_dispatch.gated_dispatch(
                    self.repo, {"changes": {self.cid: {"timeout_minutes": 1}}},
                    self.gate(job_id), self.cid, "implement", 1,
                    {"escalation": {"active": False}}, "run-1",
                    resolved_model="openai/gpt-4o",
                )
        self.assertEqual(ctx.exception.gate, journal_dispatch.GATE_BUDGET)
        self.assertEqual(ctx.exception.last_result, "budget_exhausted")
        self.assertEqual(self.ledger.reservations_for_job(job_id), [])
        self.assertEqual(self.ledger.dispatch_intervals(job_id), [])
        actions = self.ledger.list_actions(job_id)
        self.assertEqual(len(actions), 1)
        self.assertEqual(actions[0]["state"], "failed")


class FreshnessTests(ActionJournalTestCase):
    def test_on_disk_manifest_mutation_blocks_dispatch(self) -> None:
        job_id = self.register()
        self.manifest_path.write_text(MANIFEST + "# mutated\n", encoding="utf-8")
        with self.fenced(job_id):
            with self.assertRaises(journal_dispatch.StaleMaterialGateError) as ctx:
                journal_dispatch.gated_dispatch(
                    self.repo, {"changes": {self.cid: {"timeout_minutes": 1}}},
                    self.gate(job_id), self.cid, "implement", 1,
                    {"escalation": {"active": False}}, "run-1",
                    resolved_model="openai/gpt-4o",
                )
        self.assertIn("current plan on disk", str(ctx.exception))
        self.assertEqual(self.ledger.list_actions(job_id), [])

    def test_policy_revision_change_blocks_next_dispatch(self) -> None:
        job_id = self.register()
        self.dispatch(job_id, stage="create")
        gate = self.gate(job_id)
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
        with self.fenced(job_id):
            with self.assertRaises(journal_dispatch.StaleMaterialGateError):
                journal_dispatch.gated_dispatch(
                    self.repo, {"changes": {self.cid: {"timeout_minutes": 1}}},
                    gate, self.cid, "implement", 1,
                    {"escalation": {"active": False}}, "run-1",
                )

    def test_changed_manifest_snapshot_hash_blocks_dispatch(self) -> None:
        job_id = self.register()
        gate = self.gate(job_id)
        gate["manifest_snapshot_hash"] = "a-different-snapshot-hash"
        with self.fenced(job_id):
            with self.assertRaises(journal_dispatch.StaleMaterialGateError):
                journal_dispatch.gated_dispatch(
                    self.repo, {"changes": {self.cid: {"timeout_minutes": 1}}},
                    gate, self.cid, "implement", 1,
                    {"escalation": {"active": False}}, "run-1",
                )

    def test_unrelated_file_update_does_not_block(self) -> None:
        job_id = self.register()
        (self.repo / "unrelated.txt").write_text("ignore me\n", encoding="utf-8")
        entry = self.dispatch(job_id, stage="create")
        self.assertIn("action_id", entry)


class UncertaintyTests(ActionJournalTestCase):
    def test_lost_worker_marks_uncertain_and_blocks_until_reconciled(self) -> None:
        job_id = self.register()
        entry = self.dispatch(job_id)
        action_id = entry["action_id"]
        gate = self.gate(job_id)
        journal_dispatch.resolve_dispatch(
            gate, action_id=action_id, reservation_id=entry["reservation_id"],
            outcome="timeout", record=None,
        )
        self.assertEqual(self.ledger.get_action(action_id)["state"], "uncertain")
        pending = journal_dispatch.reconcile_pending(self.ledger, job_id)
        self.assertEqual([item["action_id"] for item in pending], [action_id])
        self.assertEqual(pending[0]["change_id"], self.cid)
        self.assertEqual(pending[0]["stage"], "implement")

        # Recorded decisive evidence reconciles the action to its terminal state.
        self.ledger.record_evidence(
            action_id, kind="stage_result",
            payload={"confirmed": True, "outcome": "completed"},
        )
        outcome = journal_dispatch.replay_uncertain(
            self.repo,
            {"changes": {self.cid: {"timeout_minutes": 1}}},
            gate,
            action_id,
        )
        self.assertEqual(outcome.get("replayed"), False)
        self.assertEqual(self.ledger.get_action(action_id)["state"], "completed")
        self.assertEqual(journal_dispatch.reconcile_pending(self.ledger, job_id), [])

    def test_uncertain_action_inventory_retains_orphan_reservation(self) -> None:
        job_id = self.register()
        entry = self.dispatch(job_id)
        action_id = entry["action_id"]
        self.ledger.mark_uncertain(action_id, detail="worker disappeared")

        pending = journal_dispatch.reconcile_pending(self.ledger, job_id)

        self.assertEqual([item["action_id"] for item in pending], [action_id])
        reservation = self.ledger.get_reservation(entry["reservation_id"])
        self.assertEqual(reservation["state"], "retained")

    def test_dispatched_action_is_surfaced_as_uncertain_on_resume(self) -> None:
        job_id = self.register()
        entry = self.dispatch(job_id)

        pending = journal_dispatch.reconcile_pending(self.ledger, job_id)

        self.assertEqual([item["action_id"] for item in pending], [entry["action_id"]])
        self.assertEqual(self.ledger.get_action(entry["action_id"])["state"], "uncertain")
        self.assertEqual(pending[0]["change_id"], self.cid)
        self.assertEqual(pending[0]["stage"], "implement")
        reservation = self.ledger.get_reservation(entry["reservation_id"])
        self.assertEqual(reservation["state"], "retained")


class OutcomeResolutionTests(ActionJournalTestCase):
    def test_confirmed_worker_failure_becomes_failed(self) -> None:
        job_id = self.register()
        entry = self.dispatch(job_id)
        journal_dispatch.record_session_binding(
            self.ledger, entry["action_id"], "confirmed-worker"
        )
        disposition = journal_dispatch.resolve_dispatch(
            self.gate(job_id), action_id=entry["action_id"],
            reservation_id=entry["reservation_id"], outcome="failed", record=None,
        )
        self.assertEqual(disposition, "failed")
        self.assertEqual(self.ledger.get_action(entry["action_id"])["state"], "failed")


class ReplayDisciplineTests(ActionJournalTestCase):
    def _uncertain_action(self, job_id: int) -> int:
        entry = self.dispatch(job_id)
        action_id = entry["action_id"]
        self.ledger.bind_dispatch_identity(
            action_id,
            process_id=json.dumps({
                "pid": 99999999,
                "process_start": 1.0,
                "boot_id": lock_mod.boot_identity(),
            }),
        )
        journal_dispatch.resolve_dispatch(
            self.gate(job_id), action_id=action_id,
            reservation_id=entry["reservation_id"],
            outcome="invalid_output", record=None,
        )
        return action_id

    def test_duplicate_result_reconciles_without_double_dispatch(self) -> None:
        job_id = self.register()
        action_id = self._uncertain_action(job_id)
        # A duplicate delivered result recorded while uncertain reconciles.
        self.ledger.record_evidence(
            action_id, kind="stage_result",
            payload={"confirmed": True, "outcome": "completed"},
        )
        before = len(self.ledger.list_dispatches(action_id))
        outcome = journal_dispatch.replay_uncertain(
            self.repo,
            {"changes": {self.cid: {"timeout_minutes": 1}}},
            self.gate(job_id),
            action_id,
        )
        self.assertEqual(outcome.get("replayed"), False)
        self.assertEqual(len(self.ledger.list_dispatches(action_id)), before)

    def test_replay_only_after_reobservation_shows_incomplete(self) -> None:
        job_id = self.register()
        action_id = self._uncertain_action(job_id)
        with self.fenced(job_id):
            outcome = journal_dispatch.replay_uncertain(
                self.repo,
                {"changes": {self.cid: {"timeout_minutes": 1}}},
                self.gate(job_id), action_id,
                cid=self.cid, round_num=1,
                r={"escalation": {"active": False}}, run_id="run-1",
                resolved_model="openai/gpt-4o", reobserve=lambda: False,
            )
        self.assertTrue(outcome.get("replayed"))
        self.assertEqual(self.ledger.get_action(action_id)["state"], "failed")
        self.assertNotEqual(outcome["action_id"], action_id)
        self.assertEqual(len(self.ledger.reservations_for_job(job_id)), 2)

    def test_live_prior_worker_fences_replay(self) -> None:
        job_id = self.register()
        action_id = self._uncertain_action(job_id)
        proc = subprocess.Popen(["sleep", "30"])
        self.addCleanup(proc.wait)
        self.addCleanup(proc.terminate)
        self.ledger.bind_dispatch_identity(
            action_id, process_id=journal_dispatch.serialize_process_identity(proc.pid)
        )
        outcome = journal_dispatch.replay_uncertain(
            self.repo,
            {"changes": {self.cid: {"timeout_minutes": 1}}},
            self.gate(job_id), action_id, reobserve=lambda: False,
        )
        self.assertEqual(outcome.get("reason"), "prior_worker_live")
        self.assertEqual(len(self.ledger.list_dispatches(action_id)), 1)

    def test_replay_reenters_authority_gate_before_replacement(self) -> None:
        job_id = self.register()
        action_id = self._uncertain_action(job_id)
        with self.fenced(job_id), mock.patch.object(
            journal_dispatch, "assert_authority_gate",
            side_effect=journal_dispatch.AuthorityGateError("revoked"),
        ):
            with self.assertRaises(journal_dispatch.AuthorityGateError):
                journal_dispatch.replay_uncertain(
                    self.repo,
                    {"changes": {self.cid: {"timeout_minutes": 1}}},
                    self.gate(job_id), action_id,
                    cid=self.cid, round_num=1,
                    r={"escalation": {"active": False}}, run_id="run-1",
                    resolved_model="openai/gpt-4o", reobserve=lambda: False,
                )
        self.assertEqual(len(self.ledger.reservations_for_job(job_id)), 1)


class DispatchPathTests(ActionJournalTestCase):
    def test_subprocess_dispatch_records_process_identity(self) -> None:
        job_id = self.register()
        entry = self.dispatch(job_id)
        action_id = entry["action_id"]
        log_path = self.repo / ".opsx-plan" / "logs" / "subprocess.log"
        log_path.parent.mkdir(parents=True, exist_ok=True)
        self.opsx._load_journal_dispatch()
        self.opsx.run_logged_command(
            self.repo, ["true"], log_path, 30, "implement", 1
        )
        row = self.ledger.latest_dispatch(action_id)
        identity = journal_dispatch.parse_process_identity(row["process_id"])
        self.assertIsNotNone(identity)
        self.assertGreater(identity["pid"], 0)
        journal_dispatch.end_active_dispatch(action_id)

    def test_task_session_binding_records_session_identity(self) -> None:
        job_id = self.register()
        entry = self.dispatch(job_id)
        action_id = entry["action_id"]
        journal_dispatch.record_session_binding(self.ledger, action_id, "task-session-7")
        row = self.ledger.latest_dispatch(action_id)
        self.assertEqual(row["session_id"], "task-session-7")
        kinds = [e["kind"] for e in self.ledger.list_evidence(action_id)]
        self.assertIn("session_binding", kinds)

    def test_endpoint_evidence_for_another_job_is_refused(self) -> None:
        other = self.root / "other"
        other.mkdir()
        git(other, "init")
        (other / "t.txt").write_text("x\n", encoding="utf-8")
        git(other, "add", "t.txt")
        git(other, "-c", "user.email=t@e.invalid", "-c", "user.name=T",
            "commit", "-m", "init")
        job1 = self.register()
        job2 = self.ledger.register_job(
            run_id="run-2", worktree=other, owner="service",
            policy=_policy(), operator="operator", manifest_content=MANIFEST,
        )
        foreign_action = self.ledger.begin_action(
            job2, kind="implement", run_id="run-2"
        )
        credentials = endpoints_mod.PeerCredentials(pid=os.getpid(), uid=os.getuid(), gid=os.getgid())
        with self.assertRaises(broker_mod.BrokerMediationError):
            endpoints_mod._worker_record_evidence(
                {
                    "ledger": self.ledger,
                    "job_id": job1,
                    "action_id": foreign_action,
                    "role": "implementer",
                    "observed_agent": "opsx-implementer",
                    "service_identity": self.principal,
                },
                credentials,
            )
        self.assertEqual(self.ledger.list_evidence(foreign_action), [])

    def test_endpoint_evidence_reconciles_bound_action(self) -> None:
        job_id = self.register()
        entry = self.dispatch(job_id)
        action_id = entry["action_id"]
        self.ledger.mark_uncertain(action_id, detail="lost")
        credentials = endpoints_mod.PeerCredentials(pid=os.getpid(), uid=os.getuid(), gid=os.getgid())
        result = endpoints_mod._worker_record_evidence(
            {
                "ledger": self.ledger,
                "job_id": job_id,
                "action_id": action_id,
                "role": "implementer",
                "observed_agent": "opsx-implementer",
                "service_identity": self.principal,
                "evidence": {
                    "kind": "stage_result",
                    "payload": {"confirmed": True, "outcome": "completed"},
                },
            },
            credentials,
        )
        self.assertEqual(result["action_id"], action_id)
        self.assertEqual(self.ledger.get_action(action_id)["state"], "completed")

    def test_usage_and_session_evidence_do_not_reconcile(self) -> None:
        job_id = self.register()
        entry = self.dispatch(job_id)
        action_id = entry["action_id"]
        journal_dispatch.record_session_binding(self.ledger, action_id, "task-1")
        journal_dispatch.resolve_dispatch(
            self.gate(job_id), action_id=action_id,
            reservation_id=entry["reservation_id"],
            outcome="invalid_output", record=None,
        )
        credentials = endpoints_mod.PeerCredentials(
            pid=os.getpid(), uid=os.getuid(), gid=os.getgid()
        )
        endpoints_mod._worker_record_evidence(
            {
                "ledger": self.ledger, "job_id": job_id,
                "action_id": action_id, "kind": "usage",
                "payload": {"tokens": 10},
                "role": "implementer",
                "observed_agent": "opsx-implementer",
                "service_identity": self.principal,
            },
            credentials,
        )
        journal_dispatch.record_session_binding(self.ledger, action_id, "task-2")
        self.assertEqual(self.ledger.get_action(action_id)["state"], "uncertain")


class SpawnSafetyTests(ActionJournalTestCase):
    def _active_dispatch(self) -> tuple[int, dict, Path]:
        job_id = self.register()
        entry = self.dispatch(job_id)
        self.opsx._load_journal_dispatch()
        log_path = self.repo / ".opsx-plan" / "logs" / "failure.log"
        log_path.parent.mkdir(parents=True, exist_ok=True)
        return job_id, entry, log_path

    def test_popen_error_does_not_leave_dispatched_action(self) -> None:
        _job_id, entry, log_path = self._active_dispatch()
        with mock.patch.object(
            self.opsx.subprocess, "Popen", side_effect=PermissionError("denied")
        ):
            outcome, _ = self.opsx.run_logged_command(
                self.repo, ["missing"], log_path, 1, "implement", 1
            )
        self.assertEqual(outcome, "spawn_error")
        self.assertEqual(self.ledger.get_action(entry["action_id"])["state"], "failed")
        self.assertIsNone(journal_dispatch.active_dispatch())

    def test_wait_error_marks_action_uncertain_before_raising(self) -> None:
        _job_id, entry, log_path = self._active_dispatch()
        proc = mock.Mock(pid=os.getpid())
        proc.wait.side_effect = RuntimeError("wait failed")
        with mock.patch.object(self.opsx.subprocess, "Popen", return_value=proc), \
                mock.patch.object(self.opsx, "terminate_group"):
            with self.assertRaises(RuntimeError):
                self.opsx.run_logged_command(
                    self.repo, ["worker"], log_path, 1, "implement", 1
                )
        self.assertEqual(self.ledger.get_action(entry["action_id"])["state"], "uncertain")
        self.assertIsNone(journal_dispatch.active_dispatch())

    def test_sigint_marks_action_uncertain_before_exit(self) -> None:
        _job_id, entry, _log_path = self._active_dispatch()
        with self.assertRaises(SystemExit) as ctx:
            self.opsx.handle_sigint(None, None)
        self.assertEqual(ctx.exception.code, 130)
        self.assertEqual(self.ledger.get_action(entry["action_id"])["state"], "uncertain")

    def test_identity_binding_failure_fails_closed(self) -> None:
        _job_id, entry, log_path = self._active_dispatch()
        proc = mock.Mock(pid=os.getpid())
        with mock.patch.object(self.opsx.subprocess, "Popen", return_value=proc), \
                mock.patch.object(self.opsx, "terminate_group"), \
                mock.patch.object(
                    self.ledger, "bind_dispatch_identity",
                    side_effect=ledger.LedgerError("write failed"),
                ):
            outcome, _ = self.opsx.run_logged_command(
                self.repo, ["worker"], log_path, 1, "implement", 1
            )
        self.assertEqual(outcome, "spawn_error")
        self.assertEqual(self.ledger.get_action(entry["action_id"])["state"], "uncertain")
        self.assertIsNone(journal_dispatch.active_dispatch())

    def test_process_identity_requires_start_and_boot_fields(self) -> None:
        with mock.patch.object(lock_mod, "process_start_time", return_value=None):
            with self.assertRaises(journal_dispatch.JournalDispatchError):
                journal_dispatch.serialize_process_identity(os.getpid())
        self.assertIsNone(journal_dispatch.parse_process_identity(
            {"pid": os.getpid(), "process_start": None, "boot_id": "boot"}
        ))


class EngineIntegrationTests(ActionJournalTestCase):
    def _cfg(self) -> dict:
        return {
            "name": "run-add-journal-test",
            "_manifest_path": str(self.manifest_path),
            "adapter": "opencode",
            "implement_invoke": "opencode run --agent opsx-implementer --model openai/gpt-4o",
            "review_invoke": "opencode run --agent opsx-reviewer --model openai/gpt-4o",
            "archive_invoke": "opencode run --agent opsx-archiver --model openai/gpt-4o",
            "state_file": ".opencode/opsx-controller/{change}.json",
            "timeout_minutes": 1,
            "max_rounds": 2,
            "no_progress_limit": 2,
            "fast_checks": [],
            "check_timeout_minutes": 1,
            "require_clean_tracked": False,
            "review_created": False,
            "changes": {
                self.cid: {
                    "id": self.cid, "depends_on": [], "enabled": True,
                    "pause_before": False, "timeout_minutes": 1,
                    "create_invoke": "", "create_max_attempts": 1,
                }
            },
            "order": [self.cid],
            "created_check": "",
            "plan_doc": "",
            "create_timeout_minutes": 1,
        }

    def _write_change(self) -> None:
        cdir = self.repo / "openspec" / "changes" / self.cid
        cdir.mkdir(parents=True, exist_ok=True)
        (cdir / "proposal.md").write_text("## Why\n", encoding="utf-8")
        (cdir / "tasks.md").write_text("- [x] 1.1 done\n", encoding="utf-8")

    def test_inner_stage_dispatch_is_journaled_end_to_end(self) -> None:
        self._write_change()
        job_id = self.register()
        cfg = self._cfg()
        # Keep the invalid-output judgment to a single review dispatch: the
        # worker exited and its output was judged unusable, so the action is
        # resolved to failed rather than left uncertain.
        cfg["invalid_output_retries"] = 0
        state = {"plan": cfg["name"], "approvals": [], "changes": {}}
        saved = self.opsx.invoke_direct_stage
        self.addCleanup(setattr, self.opsx, "invoke_direct_stage", saved)
        queue = [
            {
                "status": "implemented", "change": self.cid, "round": 1,
                "progress_made": True, "completed_tasks": ["1.1"],
                "remaining_tasks": [], "task_counts": {"complete": 1, "total": 1},
                "files_touched": [], "known_change_files": [], "summary": "done",
            },
        ]

        def fake_invoke(repo, cfg_arg, cid, stage, round_num, input_block):
            context = journal_dispatch.active_dispatch()
            if context is not None:
                journal_dispatch.record_session_binding(
                    context["ledger"], context["action_id"],
                    f"fake-{stage}-{round_num}",
                )
            log_path = self.opsx.next_stage_log_path(repo, cid, stage, round_num)
            log_path.parent.mkdir(parents=True, exist_ok=True)
            if queue:
                body = json.dumps(queue.pop(0)) + "\n"
            else:
                body = "not a json envelope\n"
            log_path.write_text(body, encoding="utf-8")
            return "exited", log_path

        self.opsx.invoke_direct_stage = fake_invoke
        with self.fenced(job_id):
            self.opsx.run_direct_change(self.repo, cfg, state, self.cid)

        actions = self.ledger.list_actions(job_id)
        implement = [a for a in actions if a["kind"] == "implement"]
        self.assertEqual(len(implement), 1)
        self.assertEqual(implement[0]["state"], "completed")
        self.assertTrue(self.ledger.list_dispatches(implement[0]["id"]))
        kinds = [e["kind"] for e in self.ledger.list_evidence(implement[0]["id"])]
        self.assertIn("stage_result", kinds)
        review = [a for a in actions if a["kind"] == "review"]
        self.assertEqual(len(review), 1)
        self.assertEqual(review[0]["state"], "failed")


class LegacyPathTests(ActionJournalTestCase):
    def test_integration_module_imports_without_side_effects(self) -> None:
        """The boundary module loads on its own and touches no run state."""
        env = dict(os.environ)
        env["PYTHONPATH"] = os.pathsep.join(
            [str(SCRIPT.parents[1]), env.get("PYTHONPATH", "")]
        ).rstrip(os.pathsep)
        result = subprocess.run(
            [
                sys.executable,
                "-c",
                "from lib.orchestrator import journal_dispatch as j; "
                "print(','.join(j.EVIDENCE_KINDS))",
            ],
            cwd=self.repo,
            capture_output=True,
            text=True,
            env=env,
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(
            result.stdout.strip(), "stage_result,usage,spawn_loss,session_binding"
        )
        self.assertFalse((self.repo / ".opsx-plan").exists())
        self.assertFalse((self.repo / ".opencode").exists())

    def test_run_logged_command_does_not_import_or_invoke_journal(self) -> None:
        self.assertIsNone(self.opsx.journal_dispatch)
        log_path = self.repo / "legacy.log"
        with mock.patch.object(
            journal_dispatch, "note_spawned_process",
            side_effect=AssertionError("legacy dispatch entered journal integration"),
        ):
            outcome, _ = self.opsx.run_logged_command(
                self.repo, ["true"], log_path, 30, "implement", 1
            )
        self.assertEqual(outcome, "exited")
        self.assertIsNone(self.opsx.journal_dispatch)

    def test_unregistered_run_creates_no_journal_records(self) -> None:
        # Point at a store with no job registered: the durable layer is inert.
        EngineIntegrationTests._write_change(self)
        cfg = EngineIntegrationTests._cfg(self)
        state = {"plan": cfg["name"], "approvals": [], "changes": {}}
        saved = self.opsx.invoke_direct_stage
        self.addCleanup(setattr, self.opsx, "invoke_direct_stage", saved)

        def fake_invoke(repo, cfg_arg, cid, stage, round_num, input_block):
            log_path = self.opsx.next_stage_log_path(repo, cid, stage, round_num)
            log_path.parent.mkdir(parents=True, exist_ok=True)
            log_path.write_text("not a json envelope\n", encoding="utf-8")
            return "exited", log_path

        self.opsx.invoke_direct_stage = fake_invoke
        self.opsx.run_direct_change(self.repo, cfg, state, self.cid)
        self.assertEqual(self.ledger.list_jobs(), [])
        self.assertIsNone(self.opsx.journal_dispatch)


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
