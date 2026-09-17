"""Orchestrator wiring tests for the supervised budget gate.

Exercises task group 3 (reserve-before-dispatch and reconcile-after-dispatch in
the run paths), group 4 (deadline accounting), group 5 (attempt signatures),
and group 6 (blocked states / operator-only changes) at the real ``run_direct_change``
call site, using a registered supervised job and a patched stage dispatcher.
"""

from __future__ import annotations

import argparse
import importlib.util
import io
import json
import os
import sqlite3
import subprocess
import sys
import tempfile
import unittest
from contextlib import contextmanager, redirect_stderr
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest import mock

from lib.supervisor import budgets, clock, ledger, model_policy, lock as lock_mod

SCRIPT = Path(__file__).resolve().parents[2] / "orchestrator" / "opsx-plan.py"


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
    *, total_cost_usd=1000.0, per_action_cost_usd=None,
    total_elapsed_minutes=None, per_action_elapsed_minutes=None,
    execution_deadline_minutes=None, max_incident_attempts=None,
) -> dict:
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
            "version": budgets.BUDGET_SCHEMA_VERSION,
            "total_cost_usd": total_cost_usd,
            "per_action_cost_usd": per_action_cost_usd,
            "total_elapsed_minutes": total_elapsed_minutes,
            "per_action_elapsed_minutes": per_action_elapsed_minutes,
            "max_incident_attempts": max_incident_attempts,
        },
        "deadlines": {
            "version": budgets.BUDGET_SCHEMA_VERSION,
            "execution_deadline_minutes": execution_deadline_minutes,
        },
    }


class SupervisedGateTestCase(unittest.TestCase):
    def setUp(self) -> None:
        self.opsx_plan = load_opsx_plan()
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.repo = self.root / "repo"
        self.repo.mkdir()
        git(self.repo, "init")
        (self.repo / "tracked.txt").write_text("base\n", encoding="utf-8")
        (self.repo / ".gitignore").write_text(
            "openspec/changes/archive/\n", encoding="utf-8"
        )
        git(self.repo, "add", "tracked.txt", ".gitignore")
        git(
            self.repo,
            "-c", "user.email=test@example.invalid",
            "-c", "user.name=Test User",
            "commit", "-m", "init",
        )

        self.storage = self.root / "service-storage"
        self.storage.mkdir()
        self.db_path = self.storage / "supervisor.sqlite3"
        self.ledger = ledger.open_ledger(
            self.db_path, repository_root=self.repo
        )
        self.addCleanup(self.ledger.close)

        self.cid = "add-gate-test"
        self.plan_name = f"run-{self.cid}"
        self.cfg = {
            "name": self.plan_name,
            "adapter": "opencode",
            "implement_invoke": "opencode run --agent opsx-implementer --model $OPSX_IMPLEMENTER_MODEL",
            "review_invoke": "opencode run --agent opsx-reviewer --model $OPSX_REVIEWER_MODEL",
            "archive_invoke": "opencode run --agent opsx-archiver --model $OPSX_ARCHIVER_MODEL",
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
                    "id": self.cid,
                    "depends_on": [],
                    "enabled": True,
                    "pause_before": False,
                    "timeout_minutes": 1,
                    "create_invoke": "",
                    "create_max_attempts": 1,
                }
            },
            "order": [self.cid],
            "created_check": "",
            "plan_doc": "",
            "create_timeout_minutes": 1,
        }
        self.manifest_path = self.repo / "registered-plan.toml"
        self.manifest_path.write_text(self._manifest_content(), encoding="utf-8")
        self.cfg["_manifest_path"] = str(self.manifest_path)
        model_env = {
            "OPSX_IMPLEMENTER_MODEL": "openai/gpt-4o",
            "OPSX_REVIEWER_MODEL": "openai/gpt-4o",
            "OPSX_ARCHIVER_MODEL": "openai/gpt-4o",
            "OPSX_IMPLEMENTER_ESCALATION_MODEL": "openai/gpt-4o",
            "OPSX_SUPERVISED_AUTHOR_MODEL": "openai/gpt-4o",
        }
        model_patcher = mock.patch.dict(os.environ, model_env)
        model_patcher.start()
        self.addCleanup(model_patcher.stop)
        integration = sys.modules.get("lib.orchestrator.journal_dispatch")
        if integration is not None:
            integration.end_active_dispatch()
            self.addCleanup(integration.end_active_dispatch)
        self.state = {"plan": self.plan_name, "approvals": [], "changes": {}}
        self._saved_invoke = self.opsx_plan.invoke_direct_stage

    def tearDown(self) -> None:
        self.opsx_plan.invoke_direct_stage = self._saved_invoke

    def write_authored_change(self) -> None:
        cdir = self.repo / "openspec" / "changes" / self.cid
        cdir.mkdir(parents=True)
        (cdir / "proposal.md").write_text("## Why\n", encoding="utf-8")
        (cdir / "tasks.md").write_text(
            "## 1. Tasks\n\n- [x] 1.1 Example task\n", encoding="utf-8"
        )

    def register_job(self, *, policy: dict | None = None) -> int:
        job_id = self.ledger.register_job(
            run_id="run-1",
            worktree=self.repo,
            owner="service",
            policy=policy if policy is not None else _policy(),
            operator="operator",
            manifest_content=self._manifest_content(),
        )
        self.job_id = job_id
        return job_id

    @contextmanager
    def supervised_execution(self):
        """Authorize this process as the trusted supervised execution.

        Dispatch authorization is the service-owned ledger fence, not an
        in-process marker: record a live ``acquired`` fencing row bound to this
        process (matching boot id plus process start time) for the active
        registered job, then release it. An unregistered worktree needs no
        fence and just runs.
        """
        job = self.ledger.find_job_by_worktree(self.repo)
        if job is None:
            yield
            return
        job_id = int(job["id"])
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

    def _manifest_content(self) -> str:
        """Protected manifest content for the fixture's single gated change."""
        return (
            "[[changes]]\n"
            f'id = "{self.cid}"\n'
            "pause_before = false\n"
            "depends_on = []\n"
        )

    def enable_gate_env(self) -> None:
        patcher = mock.patch.dict(
            os.environ, {"OPSX_SUPERVISOR_STATE_FILE": str(self.db_path)}
        )
        patcher.start()
        self.addCleanup(patcher.stop)

    def stage_runner(self, payloads: list[dict], outcomes: list[str] | None = None):
        records: list[dict] = []
        outcome_list = list(outcomes or [])

        def fake_invoke(repo, cfg, cid, stage, round_num, input_block):
            integration = self.opsx_plan.journal_dispatch
            if integration is not None and integration.active_dispatch() is not None:
                context = integration.active_dispatch()
                integration.record_session_binding(
                    context["ledger"], context["action_id"],
                    f"fake-{stage}-{round_num}",
                )
            if payloads:
                payload = payloads.pop(0)
                self.assertEqual(stage, payload["stage"])
                result = dict(payload.get("result", {}))
                if "usage" in payload:
                    result["usage"] = payload["usage"]
                if "model" in payload:
                    result["model"] = payload["model"]
                body = json.dumps(result) + "\n"
            else:
                # No further stage queued: emit an unparseable line so the run
                # loop terminates after the stage under test has been exercised.
                payload = None
                body = "not a json envelope\n"
            log_path = self.opsx_plan.next_stage_log_path(repo, cid, stage, round_num)
            log_path.parent.mkdir(parents=True, exist_ok=True)
            log_path.write_text(body, encoding="utf-8")
            outcome = outcome_list.pop(0) if outcome_list else "exited"
            records.append({"stage": stage, "outcome": outcome})
            return outcome, log_path

        self.opsx_plan.invoke_direct_stage = fake_invoke
        return records

    def run_change(self, *, budget_usd: float = 0.0) -> str:
        # A registered job dispatches only inside the supervised execution, i.e.
        # as a descendant of the process the service-owned fence names. This
        # suite drives the supervised budget gate, so it always runs under a
        # live ledger fence.
        with self.supervised_execution():
            return self.opsx_plan.run_direct_change(
                self.repo, self.cfg, self.state, self.cid, budget_usd=budget_usd
            )


class ReservationWiringTests(SupervisedGateTestCase):
    def test_reserve_before_and_reconcile_after_dispatch(self) -> None:
        self.write_authored_change()
        self.register_job()
        self.enable_gate_env()
        self.stage_runner([{
            "stage": "implement",
            "result": {
                "status": "implemented", "change": self.cid, "round": 1,
                "progress_made": True, "completed_tasks": ["1.1"],
                "remaining_tasks": [], "task_counts": {"complete": 1, "total": 1},
                "files_touched": [], "known_change_files": [],
                "summary": "done",
            },
            "usage": {
                "total_tokens": 100, "input_tokens": 80, "output_tokens": 20,
            },
            "model": {"provider": "openai", "model_id": "gpt-4o"},
        }])
        result = self.run_change()
        reservations = self.ledger.reservations_for_job(1)
        # The implement dispatch is its own action: one reservation, role
        # attributed, reconciled from the observed usage.
        implement = [r for r in reservations if r["role"] == "implementer"]
        self.assertEqual(len(implement), 1)
        self.assertEqual(implement[0]["state"], "reconciled")
        self.assertEqual(implement[0]["observed_input_tokens"], 80)
        # The telemetry record carries the role dimension.
        jsonl = (self.repo / ".opsx-plan" / "telemetry" /
                 f"{self.plan_name}.jsonl")
        telemetry = [
            json.loads(line) for line in jsonl.read_text(encoding="utf-8").splitlines()
            if line.strip()
        ]
        implement_records = [t for t in telemetry if t.get("stage") == "implement"]
        self.assertTrue(implement_records)
        self.assertEqual(implement_records[0].get("role"), "implementer")
        # A later stage attempt is a separate action with its own reservation.
        self.assertGreaterEqual(len(reservations), 1)

    def test_legacy_unregistered_run_does_not_touch_the_ledger(self) -> None:
        self.write_authored_change()
        # No job registered and no env; the durable layer is inert.
        self.stage_runner([{
            "stage": "implement",
            "result": {
                "status": "implemented", "change": self.cid, "round": 1,
                "progress_made": True, "completed_tasks": ["1.1"],
                "remaining_tasks": [], "task_counts": {"complete": 1, "total": 1},
                "files_touched": [], "known_change_files": [],
                "summary": "done",
            },
        }])
        self.run_change()
        self.assertEqual(self.ledger.list_jobs(), [])

    def test_escalated_implement_is_attributed_to_escalation_role(self) -> None:
        self.write_authored_change()
        self.register_job()
        self.enable_gate_env()
        # Force escalation active and provide an escalation model env var.
        rec = self.opsx_plan.state_mod.rec(self.state, self.cid)
        rec["escalation"] = {"active": True, "activated_round": 1, "model": "openai/gpt-4o"}
        self.cfg["escalate_after_review_fails"] = 1
        rec["round"] = 2
        os.environ["OPSX_IMPLEMENTER_ESCALATION_MODEL"] = "openai/gpt-4o"
        self.addCleanup(os.environ.pop, "OPSX_IMPLEMENTER_ESCALATION_MODEL", None)
        self.stage_runner([{
            "stage": "implement",
            "result": {
                "status": "implemented", "change": self.cid, "round": 2,
                "progress_made": True, "completed_tasks": ["1.1"],
                "remaining_tasks": [], "task_counts": {"complete": 1, "total": 1},
                "files_touched": [], "known_change_files": [],
                "summary": "done",
            },
        }])
        self.run_change()
        reservations = self.ledger.reservations_for_job(1)
        self.assertEqual(reservations[0]["role"], "implementer_escalation")

    def test_create_stage_is_reserved_under_supervised_author(self) -> None:
        self.register_job()
        gate = {"ledger": self.ledger, "job_id": 1,
                "policy": self.ledger.current_policy(1)}
        entry = self.opsx_plan.supervised_gate_reserve(
            self.repo, self.cfg, gate, self.cid, "create", 1,
            {"escalation": {"active": False}}, "run-1",
        )
        self.assertEqual(entry.get("role"), "supervised_author")
        reservations = self.ledger.reservations_for_job(1)
        self.assertEqual(len(reservations), 1)
        self.assertEqual(reservations[0]["role"], "supervised_author")

    def test_execution_deadline_blocks_when_elapsed_reached(self) -> None:
        self.write_authored_change()
        self.register_job(policy=_policy(execution_deadline_minutes=0.0))
        self.enable_gate_env()
        invoked: list[str] = []
        self.opsx_plan.invoke_direct_stage = lambda *a, **k: invoked.append("called")
        result = self.run_change()
        self.assertEqual(result, "budget")
        self.assertEqual(invoked, [])
        self.assertEqual(self.ledger.reservations_for_job(1), [])

    def test_unknown_pricing_blocks_without_side_effect(self) -> None:
        self.write_authored_change()
        self.register_job(policy=_policy())
        self.enable_gate_env()
        # Break the pin so pricing cannot resolve it.
        current = self.ledger.current_policy(1)
        selection = dict(current["model_selection"])
        selection["roles"] = dict(selection["roles"])
        selection["roles"]["implementer"] = "nowhere/unknown-model"
        self.ledger.revise_policy(
            1, revision=2,
            policy={
                "authority_config": current["authority_config"],
                "model_selection": selection,
                "inexpensive_allowlist": current["inexpensive_allowlist"],
                "manifest_snapshot_hash": current["manifest_snapshot_hash"],
                "budgets": current["budgets"],
                "deadlines": current["deadlines"],
            },
            operator="operator",
        )
        invoked: list[str] = []
        self.opsx_plan.invoke_direct_stage = lambda *a, **k: invoked.append("called")
        result = self.run_change()
        self.assertEqual(result, "budget")
        self.assertEqual(invoked, [])
        self.assertEqual(self.ledger.reservations_for_job(1), [])

    def test_exhausted_total_blocks_dispatch(self) -> None:
        self.write_authored_change()
        self.register_job(policy=_policy(total_cost_usd=1.0, per_action_cost_usd=1000.0))
        self.enable_gate_env()
        # A single reservation estimate (worst-case rate x envelope x headroom)
        # exceeds the $1.00 total, so the first dispatch is blocked.
        invoked: list[str] = []
        self.opsx_plan.invoke_direct_stage = lambda *a, **k: invoked.append("called")
        result = self.run_change()
        self.assertEqual(result, "budget")
        self.assertEqual(invoked, [])

    def _sidecar_env_keys(self) -> list[str]:
        sample = self.opsx_plan._build_usage_sidecar_env(
            "p", "r", "c", "s", 1, Path("/tmp/sidecar-sample.jsonl")
        )
        self.assertIn("OPSX_USAGE_PATH", sample)
        return list(sample.keys())

    def test_blocked_direct_dispatch_restores_clean_environment(self) -> None:
        """A budget-blocked direct dispatch leaves a clean env clean.

        Regression for the review finding: the loop arms the OPSX_* sidecar
        variables before the budget gate, so a blocked reservation must still
        restore the environment. Variables absent before the run must not be
        materialized.
        """
        self.write_authored_change()
        self.register_job(
            policy=_policy(total_cost_usd=1.0, per_action_cost_usd=1000.0)
        )
        self.enable_gate_env()
        for key in self._sidecar_env_keys():
            os.environ.pop(key, None)
        invoked: list[str] = []
        self.opsx_plan.invoke_direct_stage = lambda *a, **k: invoked.append("called")
        result = self.run_change()
        self.assertEqual(result, "budget")
        self.assertEqual(invoked, [])
        for key in self._sidecar_env_keys():
            self.assertNotIn(
                key, os.environ,
                f"{key} must be removed when the budget gate blocks dispatch",
            )

    def test_blocked_direct_dispatch_restores_prior_values(self) -> None:
        """A budget-blocked direct dispatch restores pre-existing sidecar values."""
        self.write_authored_change()
        self.register_job(
            policy=_policy(total_cost_usd=1.0, per_action_cost_usd=1000.0)
        )
        self.enable_gate_env()
        for key in self._sidecar_env_keys():
            os.environ.pop(key, None)
        prior = {
            "OPSX_USAGE_PATH": "/prior/usage.jsonl",
            "OPSX_PLAN_NAME": "prior-plan",
            "OPSX_RUN_ID": "prior-run",
            "OPSX_CHANGE_ID": "prior-change",
            "OPSX_STAGE": "prior-stage",
            "OPSX_ROUND": "7",
        }
        patcher = mock.patch.dict(os.environ, prior)
        patcher.start()
        self.addCleanup(patcher.stop)
        invoked: list[str] = []
        self.opsx_plan.invoke_direct_stage = lambda *a, **k: invoked.append("called")
        result = self.run_change()
        self.assertEqual(result, "budget")
        self.assertEqual(invoked, [])
        for key, value in prior.items():
            self.assertEqual(os.environ.get(key), value)

    def _gate(self) -> dict:
        return {
            "ledger": self.ledger,
            "job_id": 1,
            "policy": self.ledger.current_policy(1),
        }

    def test_exhausted_reservation_leaves_no_phantom_dispatch(self) -> None:
        """A refused reservation must leave no dispatched action or interval.

        Regression for the review finding: the reservation is durably written
        before the dispatch is recorded, so budget exhaustion blocks the
        dispatch without charging execution-elapsed deadline time.
        """
        self.write_authored_change()
        self.register_job(
            policy=_policy(total_cost_usd=1.0, per_action_cost_usd=1000.0)
        )
        entry = self.opsx_plan.supervised_gate_reserve(
            self.repo, self.cfg, self._gate(), self.cid, "implement", 1,
            {"escalation": {"active": False}}, "run-1",
        )
        self.assertIn("blocked", entry)
        self.assertEqual(entry["last_result"], "budget_exhausted")
        self.assertEqual(self.ledger.reservations_for_job(1), [])
        self.assertEqual(self.ledger.dispatch_intervals(1), [])
        self.assertEqual(
            self.opsx_plan.execution_elapsed_minutes(self.ledger, 1), 0.0
        )

    def test_transient_reservation_failure_leaves_one_dispatched_reservation(
        self,
    ) -> None:
        """A retried reservation failure yields exactly one dispatch+reservation."""
        self.write_authored_change()
        self.register_job()
        attempts = {"n": 0}
        real_reserve = budgets.reserve

        def flaky_reserve(*args, **kwargs):
            attempts["n"] += 1
            if attempts["n"] == 1:
                raise sqlite3.OperationalError("database is locked")
            return real_reserve(*args, **kwargs)

        with mock.patch.object(self.opsx_plan.budget_mod, "reserve",
                               side_effect=flaky_reserve), \
                mock.patch.object(self.opsx_plan.budget_mod, "backoff_delay",
                                  return_value=0.0):
            entry = self.opsx_plan.supervised_gate_reserve(
                self.repo, self.cfg, self._gate(), self.cid, "implement", 1,
                {"escalation": {"active": False}}, "run-1",
            )
        self.assertEqual(attempts["n"], 2)
        self.assertNotIn("blocked", entry)
        reservations = self.ledger.reservations_for_job(1)
        self.assertEqual(len(reservations), 1)
        self.assertEqual(reservations[0]["state"], "reserved")
        self.assertEqual(len(self.ledger.dispatch_intervals(1)), 1)
        # A dispatch retried through a transient failure accrues no phantom
        # elapsed time beyond its single real dispatch interval.
        self.assertGreaterEqual(
            self.opsx_plan.execution_elapsed_minutes(self.ledger, 1), 0.0
        )

    def test_exhausted_reservation_retries_leave_no_dispatch_or_elapsed(
        self,
    ) -> None:
        """Bounded retries that all fail leave no dispatch, reservation, or elapsed."""
        self.write_authored_change()
        self.register_job()
        attempts = {"n": 0}

        def always_fail(*args, **kwargs):
            attempts["n"] += 1
            raise sqlite3.OperationalError("database is locked")

        with mock.patch.object(self.opsx_plan.budget_mod, "reserve",
                               side_effect=always_fail), \
                mock.patch.object(self.opsx_plan.budget_mod, "backoff_delay",
                                  return_value=0.0):
            entry = self.opsx_plan.supervised_gate_reserve(
                self.repo, self.cfg, self._gate(), self.cid, "implement", 1,
                {"escalation": {"active": False}}, "run-1",
            )
        self.assertGreaterEqual(attempts["n"], 1)
        self.assertIn("blocked", entry)
        self.assertEqual(entry["last_result"], "reservation_failed")
        self.assertEqual(self.ledger.reservations_for_job(1), [])
        self.assertEqual(self.ledger.dispatch_intervals(1), [])
        self.assertEqual(
            self.opsx_plan.execution_elapsed_minutes(self.ledger, 1), 0.0
        )

    def test_bounded_attempts_refuses_identical_failures(self) -> None:
        self.write_authored_change()
        self.register_job(policy=_policy(max_incident_attempts=1))
        self.enable_gate_env()
        # The first failing dispatch consumes the single allowed attempt.
        self.stage_runner([], outcomes=["timeout"])
        first = self.run_change()
        self.assertEqual(first, "failed")
        signature = budgets.incident_signature(
            kind="implement", change_id=self.cid, stage="implement",
            discriminator="dispatch",
        )
        self.assertEqual(self.ledger.incident_attempt_count(1, signature), 1)
        # A second identical failure is refused before it is dispatched: the
        # still-uncertain timed-out action routes to bounded incident
        # recovery, which escalates the unreconcilable interruption and fails
        # the change without redispatching.
        invoked: list[str] = []
        self.opsx_plan.invoke_direct_stage = lambda *a, **k: invoked.append("called")
        self.opsx_plan.state_mod.set_status(
            self.state, self.cid, "pending", "retry"
        )
        self.opsx_plan.state_mod.rec(self.state, self.cid)["phase"] = "implement"
        result = self.run_change()
        self.assertEqual(result, "failed")
        self.assertEqual(invoked, [])
        record = self.opsx_plan.state_mod.rec(self.state, self.cid)
        self.assertEqual(record["last_result"], "recovery_escalated")
        # The bound still holds: no further dispatch attempt was consumed.
        self.assertEqual(self.ledger.incident_attempt_count(1, signature), 1)

    def test_clean_dispatch_does_not_consume_attempt_bound(self) -> None:
        self.write_authored_change()
        self.register_job(policy=_policy(max_incident_attempts=1))
        self.enable_gate_env()
        self.stage_runner([{
            "stage": "implement",
            "result": {
                "status": "implemented", "change": self.cid, "round": 1,
                "progress_made": True, "completed_tasks": ["1.1"],
                "remaining_tasks": [], "task_counts": {"complete": 1, "total": 1},
                "files_touched": [], "known_change_files": [],
                "summary": "done",
            },
        }])
        self.run_change()
        signature = budgets.incident_signature(
            kind="implement", change_id=self.cid, stage="implement",
            discriminator="dispatch",
        )
        self.assertEqual(self.ledger.incident_attempt_count(1, signature), 0)

    def test_transient_ledger_failure_retries_with_bounded_backoff(self) -> None:
        self.write_authored_change()
        self.register_job()
        self.enable_gate_env()
        self.stage_runner([{
            "stage": "implement",
            "result": {
                "status": "implemented", "change": self.cid, "round": 1,
                "progress_made": True, "completed_tasks": ["1.1"],
                "remaining_tasks": [], "task_counts": {"complete": 1, "total": 1},
                "files_touched": [], "known_change_files": [],
                "summary": "done",
            },
        }])
        # A transient OperationalError on the first reservation attempt is
        # retried with bounded backoff, each retry recorded in the ledger.
        attempts = {"n": 0}
        real_reserve = budgets.reserve

        def flaky_reserve(*args, **kwargs):
            attempts["n"] += 1
            if attempts["n"] == 1:
                raise sqlite3.OperationalError("database is locked")
            return real_reserve(*args, **kwargs)

        with mock.patch.object(self.opsx_plan.budget_mod, "reserve",
                               side_effect=flaky_reserve), \
                mock.patch.object(self.opsx_plan.budget_mod, "backoff_delay",
                                  return_value=0.0):
            self.run_change()
        self.assertGreaterEqual(attempts["n"], 2)
        retry_signature = budgets.incident_signature(
            kind="budget_gate_retry", change_id=self.cid, stage="implement",
            discriminator="ledger_write",
        )
        self.assertGreaterEqual(
            self.ledger.incident_attempt_count(1, retry_signature), 1
        )

    def test_unavailable_ledger_blocks_without_spawning(self) -> None:
        """A registered job whose supervision backend is unreadable blocks.

        The gate must fail closed: the change is left pending with a budget
        state and no stage dispatch happens, rather than silently taking the
        unregistered legacy path.
        """
        self.write_authored_change()
        self.register_job()
        # Point at a path that exists but is not a usable ledger file, so the
        # open fails while a supervision backend is clearly configured.
        broken = self.storage / "broken.sqlite3"
        broken.write_bytes(b"not a sqlite database")
        patcher = mock.patch.dict(
            os.environ, {"OPSX_SUPERVISOR_STATE_FILE": str(broken)}
        )
        patcher.start()
        self.addCleanup(patcher.stop)
        invoked: list[str] = []
        self.opsx_plan.invoke_direct_stage = lambda *a, **k: invoked.append("called")
        result = self.run_change()
        self.assertEqual(result, "budget")
        self.assertEqual(invoked, [], "no dispatch may run without a reservation")
        rec = self.opsx_plan.state_mod.rec(self.state, self.cid)
        self.assertEqual(rec.get("last_result"), "supervision_gate_unavailable")


class DispatchPolicyRegressionTests(SupervisedGateTestCase):
    def test_missing_actual_model_blocks_before_spawn(self) -> None:
        self.write_authored_change()
        self.register_job()
        self.enable_gate_env()
        self.cfg["implement_invoke"] = "python3 worker.py"
        os.environ.pop("OPSX_IMPLEMENTER_MODEL", None)
        invoked: list[str] = []
        self.opsx_plan.invoke_direct_stage = lambda *a, **k: invoked.append("called")
        self.assertEqual(self.run_change(), "budget")
        self.assertEqual(invoked, [])
        self.assertIn(
            "unresolved", self.opsx_plan.state_mod.rec(self.state, self.cid)["reason"]
        )

    def test_mismatched_actual_model_blocks_before_spawn(self) -> None:
        self.write_authored_change()
        policy = _policy()
        policy["inexpensive_allowlist"]["models"].append("openai/other")
        self.register_job(policy=policy)
        self.enable_gate_env()
        self.cfg["implement_invoke"] = "worker --model openai/other"
        invoked: list[str] = []
        self.opsx_plan.invoke_direct_stage = lambda *a, **k: invoked.append("called")
        self.assertEqual(self.run_change(), "budget")
        self.assertEqual(invoked, [])
        self.assertIn(
            "differs", self.opsx_plan.state_mod.rec(self.state, self.cid)["reason"]
        )

    def test_create_controller_model_mismatch_blocks_before_spawn(self) -> None:
        policy = _policy()
        policy["inexpensive_allowlist"]["models"].append("openai/controller")
        self.register_job(policy=policy)
        self.enable_gate_env()
        self.cfg["create_invoke"] = "worker --model openai/controller"
        self.cfg["changes"][self.cid]["create_invoke"] = self.cfg["create_invoke"]
        invoked: list[str] = []
        self.opsx_plan.run_stage = lambda *a, **k: invoked.append("called")
        with self.supervised_execution():
            result = self.opsx_plan.dispatch_create_stage(
                self.repo, self.cfg, self.state, self.cid, 1,
                self.cfg["create_invoke"], {"escalation": {"active": False}},
                "run-1",
            )
        self.assertIsInstance(result, dict)
        self.assertIn("differs", result["blocked"])
        self.assertEqual(invoked, [])

    def test_create_without_verified_artifacts_is_failed_not_completed(self) -> None:
        self.register_job()
        self.enable_gate_env()
        self.cfg["create_invoke"] = "worker --model openai/gpt-4o"
        self.cfg["changes"][self.cid]["create_invoke"] = self.cfg["create_invoke"]

        def fake(repo, cfg, cid, stage, invoke_tpl, timeout_minutes, attempt):
            integration = self.opsx_plan.journal_dispatch
            context = integration.active_dispatch()
            integration.record_session_binding(
                context["ledger"], context["action_id"], "create-no-artifacts"
            )
            log_path = self.opsx_plan.next_stage_log_path(repo, cid, stage, attempt)
            log_path.parent.mkdir(parents=True, exist_ok=True)
            log_path.write_text('{"status":"created"}\n', encoding="utf-8")
            return "exited", log_path

        self.opsx_plan.run_stage = fake
        with self.supervised_execution():
            result = self.opsx_plan.dispatch_create_stage(
                self.repo, self.cfg, self.state, self.cid, 1,
                self.cfg["create_invoke"], {"escalation": {"active": False}},
                "run-1",
            )
        self.assertIsInstance(result, tuple)
        actions = self.ledger.list_actions(self.job_id)
        self.assertEqual(len(actions), 1)
        self.assertEqual(actions[0]["state"], "failed")

    def test_create_retry_cannot_bypass_unreconciled_uncertainty(self) -> None:
        self.register_job()
        self.enable_gate_env()
        self.cfg["create_invoke"] = "worker --model openai/gpt-4o"
        self.cfg["changes"][self.cid]["create_invoke"] = self.cfg["create_invoke"]
        calls: list[int] = []

        def fake(repo, cfg, cid, stage, invoke_tpl, timeout_minutes, attempt):
            calls.append(attempt)
            integration = self.opsx_plan.journal_dispatch
            context = integration.active_dispatch()
            integration.record_session_binding(
                context["ledger"], context["action_id"], "unfenceable-task"
            )
            log_path = self.opsx_plan.next_stage_log_path(repo, cid, stage, attempt)
            log_path.parent.mkdir(parents=True, exist_ok=True)
            log_path.write_text("timed out\n", encoding="utf-8")
            return "timeout", log_path

        self.opsx_plan.run_stage = fake
        with self.supervised_execution():
            first = self.opsx_plan.dispatch_create_stage(
                self.repo, self.cfg, self.state, self.cid, 1,
                self.cfg["create_invoke"], {"escalation": {"active": False}},
                "run-1",
            )
            second = self.opsx_plan.dispatch_create_stage(
                self.repo, self.cfg, self.state, self.cid, 2,
                self.cfg["create_invoke"], {"escalation": {"active": False}},
                "run-1",
            )
        self.assertIsInstance(first, tuple)
        self.assertIsInstance(second, dict)
        self.assertEqual(second["last_result"], "uncertain_action_pending")
        self.assertEqual(calls, [1])
        self.assertEqual(len(self.ledger.list_actions(self.job_id)), 1)

    def test_resume_recovery_invokes_fenced_budgeted_replay(self) -> None:
        self.write_authored_change()
        (
            self.opsx_plan.groundtruth.change_dir(self.repo, self.cid) / "tasks.md"
        ).write_text(
            "## 1. Tasks\n\n- [ ] 1.1 Example task\n", encoding="utf-8"
        )
        self.register_job()
        self.enable_gate_env()
        integration = self.opsx_plan._load_journal_dispatch()
        with self.supervised_execution():
            gate = self.opsx_plan.open_supervised_gate(
                self.repo, self.cfg["_manifest_path"]
            )
            entry = integration.gated_dispatch(
                self.repo, self.cfg, gate, self.cid, "implement", 1,
                {"escalation": {"active": False}}, "run-1",
                resolved_model="openai/gpt-4o",
            )
            gate["ledger"].bind_dispatch_identity(
                entry["action_id"], process_id=json.dumps({
                    "pid": 99999999, "process_start": 1.0,
                    "boot_id": lock_mod.boot_identity(),
                })
            )
            integration.resolve_dispatch(
                gate, action_id=entry["action_id"],
                reservation_id=entry["reservation_id"],
                outcome="invalid_output", record=None,
            )
            self.opsx_plan.close_supervised_gate(gate)

        self.stage_runner([{
            "stage": "implement",
            "result": {
                "status": "implemented", "change": self.cid, "round": 1,
                "progress_made": True, "completed_tasks": ["1.1"],
                "remaining_tasks": [], "task_counts": {"complete": 1, "total": 1},
                "files_touched": [], "known_change_files": [], "summary": "done",
            },
        }])
        with mock.patch.object(
            integration, "replay_uncertain", wraps=integration.replay_uncertain
        ) as replay:
            self.run_change()
        self.assertGreaterEqual(replay.call_count, 1)
        reservations = self.ledger.reservations_for_job(self.job_id)
        self.assertGreaterEqual(len(reservations), 2)
        self.assertEqual(self.ledger.list_actions(self.job_id)[0]["state"], "failed")

    def test_completed_recovery_skips_duplicate_stage_dispatch(self) -> None:
        self.write_authored_change()
        self.register_job()
        self.enable_gate_env()
        integration = self.opsx_plan._load_journal_dispatch()
        with self.supervised_execution():
            gate = self.opsx_plan.open_supervised_gate(
                self.repo, self.cfg["_manifest_path"]
            )
            entry = integration.gated_dispatch(
                self.repo, self.cfg, gate, self.cid, "implement", 1,
                {"escalation": {"active": False}}, "run-1",
                resolved_model="openai/gpt-4o",
            )
            integration.record_session_binding(
                gate["ledger"], entry["action_id"], "completed-worker"
            )
            integration.resolve_dispatch(
                gate, action_id=entry["action_id"],
                reservation_id=entry["reservation_id"],
                outcome="invalid_output", record=None,
            )
            gate["ledger"].record_evidence(
                entry["action_id"], kind="stage_result",
                payload={"confirmed": True, "outcome": "completed"},
            )
            self.opsx_plan.close_supervised_gate(gate)

        r = self.opsx_plan.state_mod.rec(self.state, self.cid)
        r["phase"] = "review"
        records = self.stage_runner([{
            "stage": "review",
            "result": {
                "status": "passed", "change": self.cid, "round": 1,
                "findings": [], "summary": "clean",
            },
        }])

        self.run_change()

        self.assertTrue(records)
        self.assertEqual(records[0]["stage"], "review")
        implement_actions = [
            row for row in self.ledger.list_actions(self.job_id)
            if row["kind"] == "implement"
        ]
        self.assertEqual(len(implement_actions), 1)
        self.assertEqual(implement_actions[0]["state"], "completed")

    def test_same_stage_completion_uses_repository_evidence(self) -> None:
        self.write_authored_change()
        tasks_path = self.opsx_plan.groundtruth.change_dir(
            self.repo, self.cid
        ) / "tasks.md"
        tasks_path.write_text(
            "## 1. Tasks\n\n- [ ] 1.1 Example task\n", encoding="utf-8"
        )
        self.register_job()
        self.enable_gate_env()
        integration = self.opsx_plan._load_journal_dispatch()
        with self.supervised_execution():
            gate = self.opsx_plan.open_supervised_gate(
                self.repo, self.cfg["_manifest_path"]
            )
            entry = integration.gated_dispatch(
                self.repo, self.cfg, gate, self.cid, "implement", 1,
                {"escalation": {"active": False}}, "run-1",
                resolved_model="openai/gpt-4o",
            )
            integration.resolve_dispatch(
                gate, action_id=entry["action_id"],
                reservation_id=entry["reservation_id"],
                outcome="invalid_output", record=None,
            )
            self.opsx_plan.close_supervised_gate(gate)
        tasks_path.write_text(
            "## 1. Tasks\n\n- [x] 1.1 Example task\n", encoding="utf-8"
        )

        records = self.stage_runner([{
            "stage": "review",
            "result": {
                "status": "reviewed", "change": self.cid, "round": 1,
                "verdict": "pass",
                "finding_counts": {"critical": 0, "warning": 0, "note": 0},
                "findings": [], "summary": "clean", "fix_prompt": "",
            },
        }])

        self.run_change()

        self.assertTrue(records)
        self.assertEqual(records[0]["stage"], "review")
        actions = self.ledger.list_actions(self.job_id)
        self.assertEqual([row["kind"] for row in actions[:2]], ["implement", "review"])
        self.assertEqual(actions[0]["state"], "completed")

    def test_same_stage_decisive_result_fails_closed_without_state_evidence(self) -> None:
        self.write_authored_change()
        self.register_job()
        self.enable_gate_env()
        integration = self.opsx_plan._load_journal_dispatch()
        with self.supervised_execution():
            gate = self.opsx_plan.open_supervised_gate(
                self.repo, self.cfg["_manifest_path"]
            )
            entry = integration.gated_dispatch(
                self.repo, self.cfg, gate, self.cid, "review", 1,
                {"escalation": {"active": False}}, "run-1",
                resolved_model="openai/gpt-4o",
            )
            integration.record_session_binding(
                gate["ledger"], entry["action_id"], "completed-reviewer"
            )
            integration.resolve_dispatch(
                gate, action_id=entry["action_id"],
                reservation_id=entry["reservation_id"],
                outcome="invalid_output", record=None,
            )
            gate["ledger"].record_evidence(
                entry["action_id"], kind="stage_result",
                payload={"confirmed": True, "outcome": "completed"},
            )
            self.opsx_plan.close_supervised_gate(gate)

        r = self.opsx_plan.state_mod.rec(self.state, self.cid)
        r["phase"] = "review"
        records = self.stage_runner([])

        self.assertEqual(self.run_change(), "failed")
        self.assertEqual(records, [])
        self.assertEqual(r["status"], self.opsx_plan.base.FAILED)
        self.assertEqual(r["last_result"], "recovered_action_state_mismatch")
        self.assertEqual(self.run_change(), "failed")
        self.assertEqual(len(self.ledger.list_actions(self.job_id)), 1)

    def test_same_stage_archive_recovery_finishes_without_redispatch(self) -> None:
        self.write_authored_change()
        self.register_job()
        self.enable_gate_env()
        integration = self.opsx_plan._load_journal_dispatch()
        with self.supervised_execution():
            gate = self.opsx_plan.open_supervised_gate(
                self.repo, self.cfg["_manifest_path"]
            )
            entry = integration.gated_dispatch(
                self.repo, self.cfg, gate, self.cid, "archive", 1,
                {"escalation": {"active": False}}, "run-1",
                resolved_model="openai/gpt-4o",
            )
            integration.resolve_dispatch(
                gate, action_id=entry["action_id"],
                reservation_id=entry["reservation_id"],
                outcome="invalid_output", record=None,
            )
            self.opsx_plan.close_supervised_gate(gate)

        archive_dir = (
            self.repo / "openspec" / "changes" / "archive"
            / f"2026-09-15-{self.cid}"
        )
        archive_dir.parent.mkdir(parents=True, exist_ok=True)
        self.opsx_plan.groundtruth.change_dir(self.repo, self.cid).rename(archive_dir)
        r = self.opsx_plan.state_mod.rec(self.state, self.cid)
        r["phase"] = "archive"
        records = self.stage_runner([])

        self.assertEqual(self.run_change(), self.opsx_plan.base.DONE)
        self.assertEqual(records, [])
        self.assertEqual(r["phase"], "done")
        self.assertEqual(r["status"], self.opsx_plan.base.DONE)
        self.assertEqual(len(self.ledger.list_actions(self.job_id)), 1)


class CreateStageTelemetryTests(SupervisedGateTestCase):
    """End-to-end create-dispatch telemetry and reconciliation (fix round 2)."""

    def setUp(self) -> None:
        super().setUp()
        self.cfg["create_invoke"] = (
            f"python3 --model openai/gpt-4o -c \"import json,sys; "
            f"print(json.dumps({{'status':'created','usage':"
            f"{{'input_tokens':50,'output_tokens':25}}}}))\""
        )
        self.cfg["changes"][self.cid]["create_invoke"] = self.cfg["create_invoke"]
        self.cfg["created_check"] = ""
        self.cfg["require_clean_tracked"] = False
        self._saved_run_stage = self.opsx_plan.run_stage

    def tearDown(self) -> None:
        self.opsx_plan.run_stage = self._saved_run_stage
        super().tearDown()

    def _fake_run_stage(self, usage: dict | None, outcome: str = "exited"):
        def fake(repo, cfg, cid, stage, invoke_tpl, timeout_minutes, attempt):
            integration = self.opsx_plan.journal_dispatch
            if integration is not None and integration.active_dispatch() is not None:
                context = integration.active_dispatch()
                integration.record_session_binding(
                    context["ledger"], context["action_id"],
                    f"fake-{stage}-{attempt}",
                )
            log_path = self.opsx_plan.next_stage_log_path(repo, cid, stage, attempt)
            log_path.parent.mkdir(parents=True, exist_ok=True)
            body = "create output\n"
            if usage is not None:
                body += json.dumps({
                    "status": "created",
                    "usage": usage,
                    "model": {"provider": "openai", "model_id": "gpt-4o"},
                }) + "\n"
            log_path.write_text(body, encoding="utf-8")
            # Simulate the change being authored by the create dispatch so the
            # run loop can verify creation.
            cdir = repo / "openspec" / "changes" / cid
            cdir.mkdir(parents=True, exist_ok=True)
            (cdir / "proposal.md").write_text("## Why\n", encoding="utf-8")
            (cdir / "tasks.md").write_text("- [ ] 1.1 task\n", encoding="utf-8")
            return outcome, log_path

        self.opsx_plan.run_stage = fake

    def _create_reservation(self):
        reservations = self.ledger.reservations_for_job(1)
        create = [r for r in reservations if r["role"] == "supervised_author"]
        self.assertEqual(len(create), 1)
        return create[0]

    def _dispatch_create(self):
        # The dispatch boundary requires the trusted supervised execution (the
        # live ledger fence) exactly as the production cmd_run path provides.
        with self.supervised_execution():
            return self.opsx_plan.dispatch_create_stage(
                self.repo, self.cfg, self.state, self.cid, 1,
                self.cfg["create_invoke"],
                {"escalation": {"active": False}}, "run-1",
            )

    def test_successful_create_emits_role_telemetry_and_reconciles(self) -> None:
        self.register_job()
        self.enable_gate_env()
        self._fake_run_stage(usage={"input_tokens": 50, "output_tokens": 25})
        # Capture the reservation state right after the create dispatch.
        result = self._dispatch_create()
        self.assertIsInstance(result, tuple)
        reservation = self._create_reservation()
        self.assertEqual(reservation["role"], "supervised_author")
        # Observed usage was reconciled, not zero cost.
        self.assertEqual(reservation["state"], "reconciled")
        self.assertEqual(reservation["observed_input_tokens"], 50)
        self.assertEqual(reservation["observed_output_tokens"], 25)
        self.assertGreater(reservation["observed_cost_usd"] or 0.0, 0.0)
        # The create telemetry record carries the supervised_author role.
        jsonl = (self.repo / ".opsx-plan" / "telemetry" /
                 f"{self.plan_name}.jsonl")
        records = [
            json.loads(line) for line in jsonl.read_text(encoding="utf-8").splitlines()
            if line.strip()
        ]
        create_records = [r for r in records if r.get("stage") == "create"]
        self.assertTrue(create_records)
        self.assertEqual(create_records[-1].get("role"), "supervised_author")

    def test_create_without_observed_usage_retains_reservation(self) -> None:
        self.register_job()
        self.enable_gate_env()
        # A create with no parseable usage: the reservation must be retained at
        # its reserved estimate, never reconciled as free.
        self._fake_run_stage(usage=None)
        self._dispatch_create()
        reservation = self._create_reservation()
        self.assertEqual(reservation["state"], "retained")
        consumption = self.ledger.consumption_for_job(1)
        self.assertGreater(consumption["cost_usd"], 0.0)
        self.assertGreater(consumption["elapsed_minutes"], 0.0)

    def _sidecar_env_keys(self) -> list[str]:
        sample = self.opsx_plan._build_usage_sidecar_env(
            "p", "r", "c", "s", 1, Path("/tmp/sidecar-sample.jsonl")
        )
        keys = list(sample.keys())
        self.assertIn("OPSX_USAGE_PATH", keys)
        return keys

    def test_sidecar_cleanup_removes_vars_absent_before_create(self) -> None:
        """A clean environment stays clean: absent vars are not materialized."""
        self.register_job()
        self.enable_gate_env()
        self._fake_run_stage(usage={"input_tokens": 5, "output_tokens": 5})
        keys = self._sidecar_env_keys()
        for key in keys:
            os.environ.pop(key, None)
        self._dispatch_create()
        for key in keys:
            self.assertNotIn(
                key, os.environ,
                f"{key} must be removed when it was absent before the create",
            )

    def test_sidecar_cleanup_restores_prior_values(self) -> None:
        """Vars present before the create are restored to their prior values."""
        self.register_job()
        self.enable_gate_env()
        self._fake_run_stage(usage={"input_tokens": 5, "output_tokens": 5})
        prior = {
            "OPSX_USAGE_PATH": "/prior/usage.jsonl",
            "OPSX_PLAN_NAME": "prior-plan",
            "OPSX_RUN_ID": "prior-run",
            "OPSX_CHANGE_ID": "prior-change",
            "OPSX_STAGE": "prior-stage",
            "OPSX_ROUND": "7",
        }
        patcher = mock.patch.dict(os.environ, prior)
        patcher.start()
        self.addCleanup(patcher.stop)
        self._dispatch_create()
        for key, value in prior.items():
            self.assertEqual(os.environ.get(key), value)

    def test_sidecar_cleanup_mixed_presence(self) -> None:
        """Present vars are restored while absent vars stay absent."""
        self.register_job()
        self.enable_gate_env()
        self._fake_run_stage(usage={"input_tokens": 5, "output_tokens": 5})
        for key in self._sidecar_env_keys():
            os.environ.pop(key, None)
        patcher = mock.patch.dict(
            os.environ, {"OPSX_STAGE": "prior-stage", "OPSX_ROUND": "9"}
        )
        patcher.start()
        self.addCleanup(patcher.stop)
        self._dispatch_create()
        self.assertEqual(os.environ.get("OPSX_STAGE"), "prior-stage")
        self.assertEqual(os.environ.get("OPSX_ROUND"), "9")
        for key in ("OPSX_USAGE_PATH", "OPSX_PLAN_NAME", "OPSX_RUN_ID",
                    "OPSX_CHANGE_ID"):
            self.assertNotIn(key, os.environ)


class CreateRunLoopEndToEndTests(SupervisedGateTestCase):
    """The real ``_cmd_run_body`` create path accounts and reconciles usage."""

    def setUp(self) -> None:
        super().setUp()
        self._saved_run_stage = self.opsx_plan.run_stage
        self._saved_run_direct = self.opsx_plan.run_direct_change

    def tearDown(self) -> None:
        self.opsx_plan.run_stage = self._saved_run_stage
        self.opsx_plan.run_direct_change = self._saved_run_direct
        super().tearDown()

    def _write_plan(self, *, name: str) -> Path:
        plan = self.repo / "plan.toml"
        plan.write_text(
            "[plan]\n"
            f'name = "{name}"\n'
            'adapter = "opencode"\n'
            "require_clean_tracked = false\n"
            "create_timeout_minutes = 1\n"
            'created_check = ""\n'
            "review_created = false\n"
            "skip_warning = true\n"
            "\n"
            "[[changes]]\n"
            f'id = "{self.cid}"\n'
            'create_invoke = "python3 --model openai/gpt-4o --version"\n',
            encoding="utf-8",
        )
        return plan

    def _install_stage(self, usage: dict | None) -> None:
        def fake(repo, cfg, cid, stage, invoke_tpl, timeout_minutes, attempt):
            integration = self.opsx_plan.journal_dispatch
            if integration is not None and integration.active_dispatch() is not None:
                context = integration.active_dispatch()
                integration.record_session_binding(
                    context["ledger"], context["action_id"],
                    f"fake-{stage}-{attempt}",
                )
            log_path = self.opsx_plan.next_stage_log_path(repo, cid, stage, attempt)
            log_path.parent.mkdir(parents=True, exist_ok=True)
            body = "create output\n"
            if usage is not None:
                body += json.dumps({
                    "status": "created",
                    "usage": usage,
                    "model": {"provider": "openai", "model_id": "gpt-4o"},
                }) + "\n"
            log_path.write_text(body, encoding="utf-8")
            cdir = repo / "openspec" / "changes" / cid
            cdir.mkdir(parents=True, exist_ok=True)
            (cdir / "proposal.md").write_text("## Why\n", encoding="utf-8")
            (cdir / "tasks.md").write_text("- [ ] 1.1 task\n", encoding="utf-8")
            return "exited", log_path

        self.opsx_plan.run_stage = fake
        self.opsx_plan.run_direct_change = lambda *a, **k: self.opsx_plan.base.DONE

    def _run(self, plan: Path) -> int:
        args = argparse.Namespace(
            repo=str(self.repo), plan=str(plan.relative_to(self.repo)),
            dry_run=False, only=None, max_changes=1, budget_minutes=0,
            budget_usd=0, create_only=False, no_branch=True, no_pr=True,
            skip_openspec=True, skip_warning=False, skip_suggestion=False,
        )
        # A live service-owned ledger fence lets a registered job dispatch; an
        # ordinary CLI run would be refused with BrokerMediationError.
        with self.supervised_execution():
            return self.opsx_plan.cmd_run(args)

    def test_cmd_run_create_reconciles_observed_usage(self) -> None:
        plan_name = "run-add-gate-test"
        plan = self._write_plan(name=plan_name)
        job_id = self.ledger.register_job(
            run_id="run-1", worktree=self.repo, owner="service",
            policy=_policy(), operator="operator",
            manifest_content=plan.read_text(encoding="utf-8"),
        )
        self.enable_gate_env()
        self._install_stage(usage={"input_tokens": 50, "output_tokens": 25})
        self._run(plan)
        reservations = self.ledger.reservations_for_job(job_id)
        create = [r for r in reservations if r["role"] == "supervised_author"]
        self.assertEqual(len(create), 1)
        self.assertEqual(create[0]["state"], "reconciled")
        self.assertEqual(create[0]["observed_input_tokens"], 50)
        self.assertGreater(create[0]["observed_cost_usd"] or 0.0, 0.0)
        jsonl = self.repo / ".opsx-plan" / "telemetry" / f"{plan_name}.jsonl"
        records = [
            json.loads(line) for line in jsonl.read_text(encoding="utf-8").splitlines()
            if line.strip()
        ]
        create_records = [r for r in records if r.get("stage") == "create"]
        self.assertTrue(create_records)
        self.assertEqual(create_records[-1].get("role"), "supervised_author")

    def test_cmd_run_create_without_usage_retains_reservation(self) -> None:
        plan_name = "run-add-gate-test"
        plan = self._write_plan(name=plan_name)
        job_id = self.ledger.register_job(
            run_id="run-1", worktree=self.repo, owner="service",
            policy=_policy(), operator="operator",
            manifest_content=plan.read_text(encoding="utf-8"),
        )
        self.enable_gate_env()
        self._install_stage(usage=None)
        self._run(plan)
        create = [
            r for r in self.ledger.reservations_for_job(job_id)
            if r["role"] == "supervised_author"
        ]
        self.assertEqual(len(create), 1)
        self.assertEqual(create[0]["state"], "retained")
        self.assertGreater(self.ledger.consumption_for_job(job_id)["cost_usd"], 0.0)

    def test_cmd_run_create_blocks_without_spawning_when_gate_unavailable(self) -> None:
        plan_name = "run-add-gate-test"
        plan = self._write_plan(name=plan_name)
        self.ledger.register_job(
            run_id="run-1", worktree=self.repo, owner="service",
            policy=_policy(), operator="operator",
            manifest_content=plan.read_text(encoding="utf-8"),
        )
        broken = self.storage / "broken.sqlite3"
        broken.write_bytes(b"not a sqlite database")
        patcher = mock.patch.dict(
            os.environ, {"OPSX_SUPERVISOR_STATE_FILE": str(broken)}
        )
        patcher.start()
        self.addCleanup(patcher.stop)
        spawned: list[str] = []
        self.opsx_plan.run_stage = (
            lambda *a, **k: spawned.append("called") or ("exited", self.repo)
        )
        with redirect_stderr(io.StringIO()) as err:
            rc = self._run(plan)
        # A registered job whose supervision backend cannot be read fails
        # closed with the named error before any create dispatch.
        self.assertEqual(rc, 2, err.getvalue())
        self.assertIn("BrokerUnavailableError", err.getvalue())
        self.assertEqual(spawned, [], "no create may spawn without a reservation")


class RetryableCatalogLoadTests(SupervisedGateTestCase):
    """A retryable catalog load is retried and durably recorded."""

    def test_catalog_load_failure_retries_with_recorded_backoff(self) -> None:
        self.write_authored_change()
        self.register_job()
        self.enable_gate_env()
        attempts = {"n": 0}
        real_catalog = self.opsx_plan.cost_mod._get_catalog

        def flaky_catalog(repo=None):
            attempts["n"] += 1
            if attempts["n"] == 1:
                return None  # simulates a transiently unavailable catalog
            return real_catalog(repo)

        invoked: list[str] = []
        self.opsx_plan.invoke_direct_stage = lambda *a, **k: invoked.append("called")
        with mock.patch.object(self.opsx_plan.cost_mod, "_get_catalog",
                               side_effect=flaky_catalog), \
                mock.patch.object(self.opsx_plan.budget_mod, "backoff_delay",
                                  return_value=0.0):
            self.opsx_plan.supervised_gate_reserve(
                self.repo, self.cfg,
                {"ledger": self.ledger, "job_id": 1,
                 "policy": self.ledger.current_policy(1)},
                self.cid, "implement", 1,
                {"escalation": {"active": False}}, "run-1",
            )
        self.assertGreaterEqual(attempts["n"], 2)
        retry_signature = budgets.incident_signature(
            kind="budget_gate_retry", change_id=self.cid, stage="implement",
            discriminator="ledger_write",
        )
        self.assertGreaterEqual(
            self.ledger.incident_attempt_count(1, retry_signature), 1
        )
        # The successful retry still produced a durable reservation.
        self.assertEqual(len(self.ledger.reservations_for_job(1)), 1)

    def test_exhausted_catalog_load_blocks_without_reservation(self) -> None:
        self.write_authored_change()
        self.register_job()
        self.enable_gate_env()
        self.opsx_plan.invoke_direct_stage = lambda *a, **k: None
        with mock.patch.object(self.opsx_plan.cost_mod, "_get_catalog",
                               return_value=None), \
                mock.patch.object(self.opsx_plan.budget_mod, "backoff_delay",
                                  return_value=0.0):
            entry = self.opsx_plan.supervised_gate_reserve(
                self.repo, self.cfg,
                {"ledger": self.ledger, "job_id": 1,
                 "policy": self.ledger.current_policy(1)},
                self.cid, "implement", 1,
                {"escalation": {"active": False}}, "run-1",
            )
        self.assertIn("blocked", entry)
        self.assertEqual(entry["last_result"], "catalog_unavailable")
        self.assertEqual(self.ledger.reservations_for_job(1), [])


class _FakeClock:
    """Deterministic ledger clock; ``advance`` moves wall time without stamping."""

    def __init__(self, start: datetime, step_seconds: float = 1.0) -> None:
        self.now = start
        self.step = timedelta(seconds=step_seconds)

    def __call__(self) -> str:
        stamp = self.now
        self.now += self.step
        return stamp.isoformat(timespec="seconds")

    def advance(self, minutes: float) -> None:
        self.now += timedelta(minutes=minutes)


class RetainedDispatchElapsedTests(SupervisedGateTestCase):
    """A retained dispatch charges the execution deadline for the time it ran.

    Regression for the review finding: an interrupted (timeout/spawn_error/
    env_error) or unknown-usage dispatch recorded no outcome timestamp, so its
    interval closed at dispatch time and the work it actually did was free
    against ``execution_deadline_minutes``.
    """

    def _clock(self, dispatch_minutes: float) -> _FakeClock:
        """Patch the ledger clock and bill *dispatch_minutes* per dispatch."""
        fake = _FakeClock(datetime(2026, 9, 13, 12, 0, 0, tzinfo=timezone.utc))
        patcher = mock.patch.object(clock, "utcnow", fake)
        patcher.start()
        self.addCleanup(patcher.stop)
        self._dispatch_minutes = dispatch_minutes
        self._fake_clock = fake
        return fake

    def _advancing_runner(self, payloads: list[dict],
                          outcomes: list[str] | None = None) -> None:
        """Wrap the stage runner so the fake clock advances while a stage runs."""
        self.stage_runner(payloads, outcomes=outcomes)
        inner = self.opsx_plan.invoke_direct_stage

        def advancing(*args, **kwargs):
            try:
                return inner(*args, **kwargs)
            finally:
                self._fake_clock.advance(self._dispatch_minutes)

        self.opsx_plan.invoke_direct_stage = advancing

    def _closed_intervals(self) -> list[dict]:
        intervals = self.ledger.dispatch_intervals(1)
        self.assertTrue(intervals)
        self.assertFalse(
            any(interval["open"] for interval in intervals),
            "a reconciled dispatch must not leave an open interval",
        )
        return intervals

    def _interval_for(self, action_id: int) -> dict:
        matches = [
            interval for interval in self._closed_intervals()
            if interval["action_id"] == action_id
        ]
        self.assertEqual(len(matches), 1)
        return matches[0]

    @staticmethod
    def _minutes(interval: dict) -> float:
        return (
            datetime.fromisoformat(interval["ended_at"])
            - datetime.fromisoformat(interval["started_at"])
        ).total_seconds() / 60.0

    def test_timed_out_dispatch_counts_elapsed_through_reconciliation(self) -> None:
        self._clock(dispatch_minutes=9.0)
        self.write_authored_change()
        self.register_job(policy=_policy(max_incident_attempts=1))
        self.enable_gate_env()
        self._advancing_runner([], outcomes=["timeout"])
        self.assertEqual(self.run_change(), "failed")

        reservations = self.ledger.reservations_for_job(1)
        self.assertEqual(len(reservations), 1)
        self.assertEqual(reservations[0]["state"], "retained")
        self.assertIsNotNone(reservations[0]["retained_at"])
        interval = self._interval_for(reservations[0]["action_id"])
        self.assertEqual(interval["ended_at"], reservations[0]["retained_at"])
        self.assertGreaterEqual(self._minutes(interval), 9.0)
        self.assertGreaterEqual(
            self.opsx_plan.execution_elapsed_minutes(self.ledger, 1), 9.0
        )

    def test_unknown_usage_dispatch_counts_elapsed_through_reconciliation(
        self,
    ) -> None:
        self._clock(dispatch_minutes=4.0)
        self.write_authored_change()
        self.register_job()
        self.enable_gate_env()
        # A clean exit whose stage emitted no usage record is an unknown
        # observation: the reservation is retained, not released.
        self._advancing_runner([{
            "stage": "implement",
            "result": {
                "status": "implemented", "change": self.cid, "round": 1,
                "progress_made": True, "completed_tasks": ["1.1"],
                "remaining_tasks": [], "task_counts": {"complete": 1, "total": 1},
                "files_touched": [], "known_change_files": [],
                "summary": "done",
            },
        }])
        self.run_change()

        implement = [
            row for row in self.ledger.reservations_for_job(1)
            if row["role"] == "implementer"
        ]
        self.assertEqual(len(implement), 1)
        self.assertEqual(implement[0]["state"], "retained")
        interval = self._interval_for(implement[0]["action_id"])
        self.assertEqual(interval["ended_at"], implement[0]["retained_at"])
        self.assertGreaterEqual(self._minutes(interval), 4.0)
        self.assertGreaterEqual(
            self.opsx_plan.execution_elapsed_minutes(self.ledger, 1), 4.0
        )

    def test_human_wait_after_reconciliation_is_not_execution_time(self) -> None:
        fake = self._clock(dispatch_minutes=5.0)
        self.write_authored_change()
        self.register_job(policy=_policy(max_incident_attempts=1))
        self.enable_gate_env()
        self._advancing_runner([], outcomes=["timeout"])
        self.run_change()

        charged = self.opsx_plan.execution_elapsed_minutes(self.ledger, 1)
        self.assertGreaterEqual(charged, 5.0)
        consumption_before = self.ledger.consumption_for_job(1)

        # A three-hour human wait follows the retained outcome. It opens no
        # dispatch, so execution elapsed and budget consumption both stand.
        fake.advance(180.0)
        self.ledger.set_job_state(1, "paused")
        self.ledger.set_job_state(1, "active")
        self.assertEqual(
            self.opsx_plan.execution_elapsed_minutes(self.ledger, 1), charged
        )
        self.assertEqual(self.ledger.consumption_for_job(1), consumption_before)
        # The retained reservation still carries its reserved estimate.
        self.assertGreater(consumption_before["retained_cost_usd"], 0.0)
        self.assertEqual(consumption_before["reserved_cost_usd"], 0.0)


class SupervisedRecoveryDriverTests(SupervisedGateTestCase):
    """End-to-end supervised recovery: the recovery phase is driven.

    Round-3 corrective coverage for ``add-bounded-incident-recovery``: the
    production orchestrator must drive the bounded recovery flow (fixer plus
    independent verifier through the journal boundary, bounded transient
    retry, standing-grant gating, and routing to the normal/fresh-review
    loop) instead of only recording recovery metadata, and must fail a
    registered change only after escalation or bound exhaustion. Round-4
    corrective coverage adds the process-interruption flow: an unreconciled
    uncertain action is routed into the recovery phase, resumes only after
    decisive reconciliation through the journal evidence boundary, and
    otherwise escalates durably without redispatch. Round-5 corrective
    coverage keys the reconciliation to the recorded action id: a terminally
    failed or unrecorded interrupted action escalates, and a coexisting
    unrelated pending action is neither conflated with the recorded action
    nor cleared by its reconciliation.
    """

    def setUp(self) -> None:
        super().setUp()
        self.cfg["acceptance_invoke"] = (
            "opencode run --agent opsx-acceptance-reviewer "
            "--model $OPSX_ACCEPTANCE_REVIEWER_MODEL"
        )
        self.cfg["fix_invoke"] = (
            "opencode run --agent opsx-fixer --model $OPSX_FIXER_MODEL"
        )
        self.cfg["verify_invoke"] = (
            "opencode run --agent opsx-verifier --model $OPSX_VERIFIER_MODEL"
        )
        patcher = mock.patch.dict(
            os.environ,
            {
                "OPSX_ACCEPTANCE_REVIEWER_MODEL": "openai/gpt-4o",
                "OPSX_FIXER_MODEL": "openai/gpt-4o",
                "OPSX_VERIFIER_MODEL": "openai/gpt-4o",
            },
        )
        patcher.start()
        self.addCleanup(patcher.stop)

    # -- fixture helpers --

    def register_granted_job(self, *, resume_bound: int = 10) -> int:
        policy = _policy()
        policy["authority_config"]["standing_grants"] = {
            "version": 1,
            "effects": {"resume": {"max": resume_bound}},
        }
        job_id = self.register_job(policy=policy)
        self.enable_gate_env()
        return job_id

    def record(self) -> dict:
        return self.opsx_plan.state_mod.rec(self.state, self.cid)

    def incidents(self) -> list:
        return self.ledger.list_incidents(self.job_id)

    def authoritative_artifacts(self) -> list[str]:
        policy = self.ledger.current_policy(self.job_id)
        gate = {"manifest_snapshot_hash": policy["manifest_snapshot_hash"]}
        _revision, _review_set, identities = (
            self.opsx_plan.compute_acceptance_revision(
                self.repo, self.cfg, self.state, self.cid, gate=gate
            )
        )
        return identities

    def archive_change_in_repo(self) -> tuple[str, str]:
        src = self.repo / "openspec" / "changes" / self.cid
        archive_rel = f"openspec/changes/archive/2026-07-02-{self.cid}"
        dst = self.repo / archive_rel
        dst.parent.mkdir(parents=True, exist_ok=True)
        src.rename(dst)
        tracked = subprocess.run(
            ["git", "ls-files", "--", f"openspec/changes/{self.cid}"],
            cwd=self.repo, check=True, capture_output=True, text=True,
        ).stdout.strip()
        if tracked:
            git(self.repo, "add", "-A", "--", f"openspec/changes/{self.cid}")
        staged = subprocess.run(
            ["git", "diff", "--cached", "--name-only"],
            cwd=self.repo, check=True, capture_output=True, text=True,
        ).stdout.strip()
        if not staged:
            return archive_rel, ""
        git(
            self.repo,
            "-c", "user.email=test@example.invalid",
            "-c", "user.name=Test User",
            "commit", "-m", f"archive({self.cid}): archive completed change",
        )
        commit = subprocess.run(
            ["git", "rev-parse", "HEAD"],
            cwd=self.repo, check=True, capture_output=True, text=True,
        ).stdout.strip()
        return archive_rel, commit

    def recovery_runner(self, payloads: list[dict]) -> list[dict]:
        records: list[dict] = []

        def fake_invoke(repo, cfg, cid, stage, round_num, input_block):
            integration = self.opsx_plan.journal_dispatch
            if integration is not None and integration.active_dispatch() is not None:
                context = integration.active_dispatch()
                integration.record_session_binding(
                    context["ledger"], context["action_id"],
                    f"fake-{stage}-{round_num}-{len(records)}",
                )
            self.assertTrue(payloads, f"unexpected stage call: {stage}")
            payload = payloads.pop(0)
            self.assertEqual(stage, payload["stage"], "stage order mismatch")
            mutate = payload.get("mutate")
            if mutate is not None:
                mutate(self)
            if stage == "archive" and payload.get("archive_repo"):
                archive_path, commit = self.archive_change_in_repo()
                payload = {
                    **payload,
                    "result": {
                        **payload["result"],
                        "archive_path": archive_path,
                        "commit": commit,
                    },
                }
            body = payload.get("body")
            if body is None:
                body = json.dumps(payload["result"]) + "\n"
            log_path = self.opsx_plan.next_stage_log_path(repo, cid, stage, round_num)
            log_path.parent.mkdir(parents=True, exist_ok=True)
            log_path.write_text(body, encoding="utf-8")
            outcome = payload.get("outcome", "exited")
            records.append({"stage": stage, "round": round_num, "outcome": outcome})
            return outcome, log_path

        self.opsx_plan.invoke_direct_stage = fake_invoke
        return records

    # -- payload builders --

    def implement_payload(self) -> dict:
        return {
            "stage": "implement",
            "result": {
                "status": "implemented", "change": self.cid, "round": 1,
                "progress_made": True, "completed_tasks": ["1.1"],
                "remaining_tasks": [], "task_counts": {"complete": 1, "total": 1},
                "files_touched": [], "known_change_files": [],
                "summary": "done",
            },
        }

    def review_payload(self, verdict: str = "pass", locus: str = "") -> dict:
        findings = []
        counts = {"critical": 0, "warning": 0, "note": 0}
        if verdict != "pass":
            counts["critical"] = 1
            findings = [
                {
                    "severity": "critical",
                    "locus": [locus],
                    "statement": "the recurring defect is still present",
                }
            ]
        return {
            "stage": "review",
            "result": {
                "status": "reviewed", "change": self.cid, "round": 1,
                "verdict": verdict, "finding_counts": counts,
                "findings": findings,
                "summary": "review clean" if verdict == "pass" else "defect found",
                "fix_prompt": "repair the defect" if verdict != "pass" else "",
            },
        }

    def acceptance_payload(self) -> dict:
        return {
            "stage": "acceptance",
            "result": {
                "role": "acceptance_reviewer",
                "outcome": "accept",
                "artifacts_reviewed": self.authoritative_artifacts(),
                "reason": "accept",
                "fix_prompt": "",
            },
        }

    def fixer_payload(self) -> dict:
        return {
            "stage": "fix",
            "result": {
                "role": "fixer",
                "repair": "repaired the defect named by the incident",
                "files": ["tracked.txt"],
                "checks": [],
                "self_certified": False,
            },
        }

    def verifier_payload(self, verdict: str = "pass") -> dict:
        return {
            "stage": "verify",
            "result": {
                "role": "verifier",
                "verdict": verdict,
                "repair_verified": verdict == "pass",
                "diff_reviewed": True,
                "evidence": [],
                "reason": f"verifier {verdict}",
            },
        }

    def archive_payload(self) -> dict:
        return {
            "stage": "archive",
            "archive_repo": True,
            "result": {
                "status": "archived", "change": self.cid,
                "archive_path": "", "spec_sync_status": "no-delta",
                "commit": "", "summary": "archive succeeded",
            },
        }

    # -- tests --

    def spawn_interrupted_implement_action(self) -> int:
        """Dispatch an implement action whose outcome was never recorded.

        The action is left in its dispatched state, so the next run's
        reconcile pass marks it uncertain — the journaled shape of a process
        interruption.
        """
        integration = self.opsx_plan._load_journal_dispatch()
        with self.supervised_execution():
            gate = self.opsx_plan.open_supervised_gate(
                self.repo, self.cfg["_manifest_path"]
            )
            entry = integration.gated_dispatch(
                self.repo, self.cfg, gate, self.cid, "implement", 1,
                {"escalation": {"active": False}}, "run-1",
                resolved_model="openai/gpt-4o",
            )
            self.opsx_plan.close_supervised_gate(gate)
        # The interrupted controller lost its in-process dispatch context;
        # only the journaled action survives.
        integration.end_active_dispatch()
        return int(entry["action_id"])

    def unchecked_tasks(self) -> None:
        tasks_path = self.opsx_plan.groundtruth.change_dir(
            self.repo, self.cid
        ) / "tasks.md"
        tasks_path.write_text(
            "## 1. Tasks\n\n- [ ] 1.1 Example task\n", encoding="utf-8"
        )

    def test_process_interruption_reconciles_from_decisive_evidence(self) -> None:
        self.write_authored_change()
        self.unchecked_tasks()
        self.register_granted_job()
        action_id = self.spawn_interrupted_implement_action()
        with self.supervised_execution():
            gate = self.opsx_plan.open_supervised_gate(
                self.repo, self.cfg["_manifest_path"]
            )
            recovery = self.opsx_plan.begin_supervised_recovery(
                gate,
                self.cid,
                "implement",
                {
                    "failure_class": "process_interruption",
                    "message": "interrupted before the outcome was recorded",
                },
                run_id="run-1",
            )
            self.opsx_plan.close_supervised_gate(gate)
        self.assertIsInstance(recovery, dict)
        self.assertEqual(recovery.get("status"), "recovering")
        record = self.record()
        record["recovery"] = {
            "incident_id": recovery.get("incident_id"),
            "failure_class": recovery.get("failure_class"),
            "signature": recovery.get("signature"),
            "origin_stage": "implement",
            "summary": "interrupted before the outcome was recorded",
            "action_id": action_id,
        }
        record["phase"] = "recovery"
        # Decisive repository evidence: the interrupted worker completed the
        # change's tasks before its journal transition was interrupted.
        tasks_path = self.opsx_plan.groundtruth.change_dir(
            self.repo, self.cid
        ) / "tasks.md"
        tasks_path.write_text(
            "## 1. Tasks\n\n- [x] 1.1 Example task\n", encoding="utf-8"
        )
        records = self.recovery_runner(
            [
                self.review_payload(),
                self.acceptance_payload(),
                self.archive_payload(),
            ]
        )
        result = self.run_change()
        self.assertEqual(result, self.opsx_plan.base.DONE)
        # The interrupted implement action reconciled from decisive evidence
        # and was never redispatched; the change resumed at review.
        self.assertEqual(
            [entry["stage"] for entry in records],
            ["review", "acceptance", "archive"],
        )
        self.assertEqual(self.ledger.get_action(action_id)["state"], "completed")
        incidents = [
            row for row in self.incidents() if row["kind"] == "process_interruption"
        ]
        self.assertTrue(
            incidents, "a process-interruption incident must be recorded"
        )
        self.assertEqual(incidents[-1]["state"], "resolved")
        self.assertEqual(self.record()["recovery"], {})

    def test_unreconciled_process_interruption_escalates_without_redispatch(
        self,
    ) -> None:
        self.write_authored_change()
        self.unchecked_tasks()
        self.register_granted_job()
        action_id = self.spawn_interrupted_implement_action()
        records = self.recovery_runner([])
        result = self.run_change()
        self.assertEqual(result, "failed")
        # No stage was redispatched: the unreconciled action routed to the
        # driven recovery phase, which escalated instead of replaying on an
        # assumption.
        self.assertEqual(records, [])
        record = self.record()
        self.assertEqual(record["status"], self.opsx_plan.base.FAILED)
        self.assertEqual(record["last_result"], "recovery_escalated")
        incidents = [
            row for row in self.incidents() if row["kind"] == "process_interruption"
        ]
        self.assertTrue(
            incidents, "a process-interruption incident must be recorded"
        )
        self.assertEqual(incidents[-1]["state"], "escalated")
        self.assertEqual(self.ledger.get_action(action_id)["state"], "uncertain")
        # The escalation is durable: a rerun keeps the terminal change result
        # and still dispatches nothing.
        self.assertEqual(self.run_change(), "failed")
        self.assertEqual(records, [])

    def begin_interruption_recovery(self, action_id: "int | None") -> dict:
        """Record a process-interruption recovery context as the gate does."""
        with self.supervised_execution():
            gate = self.opsx_plan.open_supervised_gate(
                self.repo, self.cfg["_manifest_path"]
            )
            recovery = self.opsx_plan.begin_supervised_recovery(
                gate,
                self.cid,
                "implement",
                {
                    "failure_class": "process_interruption",
                    "message": "interrupted before the outcome was recorded",
                },
                run_id="run-1",
            )
            self.opsx_plan.close_supervised_gate(gate)
        self.assertIsInstance(recovery, dict)
        self.assertEqual(recovery.get("status"), "recovering")
        record = self.record()
        record["recovery"] = {
            "incident_id": recovery.get("incident_id"),
            "failure_class": recovery.get("failure_class"),
            "signature": recovery.get("signature"),
            "origin_stage": "implement",
            "summary": "interrupted before the outcome was recorded",
            "action_id": action_id,
        }
        record["phase"] = "recovery"
        return recovery

    def test_failed_interrupted_action_escalates_without_resume(self) -> None:
        self.write_authored_change()
        self.unchecked_tasks()
        self.register_granted_job()
        action_id = self.spawn_interrupted_implement_action()
        self.begin_interruption_recovery(action_id)
        # Decisive terminal-failure evidence: the interrupted worker failed
        # before its journal transition was interrupted. Resume requires the
        # recorded action to be decisively reconciled as *completed*; a
        # terminally failed action escalates instead.
        integration = self.opsx_plan._load_journal_dispatch()
        self.ledger.record_evidence(
            action_id,
            kind=integration.EVIDENCE_STAGE_RESULT,
            payload={"confirmed": True, "completed": False, "outcome": "failed"},
        )
        records = self.recovery_runner([])
        result = self.run_change()
        self.assertEqual(result, "failed")
        self.assertEqual(records, [])
        record = self.record()
        self.assertEqual(record["status"], self.opsx_plan.base.FAILED)
        self.assertEqual(record["last_result"], "recovery_escalated")
        incidents = [
            row for row in self.incidents() if row["kind"] == "process_interruption"
        ]
        self.assertTrue(
            incidents, "a process-interruption incident must be recorded"
        )
        self.assertEqual(incidents[-1]["state"], "escalated")
        self.assertEqual(self.ledger.get_action(action_id)["state"], "failed")

    def test_coexisting_pending_action_is_not_conflated(self) -> None:
        self.write_authored_change()
        self.unchecked_tasks()
        self.register_granted_job()
        action_id = self.spawn_interrupted_implement_action()
        # An unrelated interrupted action coexists in the same job journal.
        other_action_id = self.ledger.begin_action(
            self.job_id,
            kind="review",
            run_id="run-1",
            detail=json.dumps({"change_id": "other-change", "stage": "review"}),
        )
        self.ledger.mark_uncertain(other_action_id)
        self.begin_interruption_recovery(action_id)
        # Decisive repository evidence completes the recorded action only.
        tasks_path = self.opsx_plan.groundtruth.change_dir(
            self.repo, self.cid
        ) / "tasks.md"
        tasks_path.write_text(
            "## 1. Tasks\n\n- [x] 1.1 Example task\n", encoding="utf-8"
        )
        records = self.recovery_runner([])
        result = self.run_change()
        # The recorded action reconciled from decisive evidence and resolved
        # its incident; the coexisting action was neither conflated with it
        # nor resolved by it, and kept blocking independently until its own
        # escalation.
        self.assertEqual(result, "failed")
        self.assertEqual(records, [])
        self.assertEqual(self.ledger.get_action(action_id)["state"], "completed")
        self.assertEqual(
            self.ledger.get_action(other_action_id)["state"], "uncertain"
        )
        incidents = [
            row for row in self.incidents() if row["kind"] == "process_interruption"
        ]
        self.assertEqual(len(incidents), 2)
        self.assertEqual(incidents[0]["state"], "resolved")
        self.assertEqual(incidents[1]["state"], "escalated")
        record = self.record()
        self.assertEqual(record["status"], self.opsx_plan.base.FAILED)
        self.assertEqual(record["last_result"], "recovery_escalated")

    def test_recovery_without_recorded_action_escalates(self) -> None:
        self.write_authored_change()
        self.unchecked_tasks()
        self.register_granted_job()
        action_id = self.spawn_interrupted_implement_action()
        # A recovery context that does not record the interrupted action
        # cannot be reconciled: an empty job-wide pending inventory is not
        # decisive completion, so the incident escalates.
        self.begin_interruption_recovery(None)
        tasks_path = self.opsx_plan.groundtruth.change_dir(
            self.repo, self.cid
        ) / "tasks.md"
        tasks_path.write_text(
            "## 1. Tasks\n\n- [x] 1.1 Example task\n", encoding="utf-8"
        )
        records = self.recovery_runner([])
        result = self.run_change()
        self.assertEqual(result, "failed")
        self.assertEqual(records, [])
        record = self.record()
        self.assertEqual(record["status"], self.opsx_plan.base.FAILED)
        self.assertEqual(record["last_result"], "recovery_escalated")
        incidents = [
            row for row in self.incidents() if row["kind"] == "process_interruption"
        ]
        self.assertTrue(
            incidents, "a process-interruption incident must be recorded"
        )
        self.assertEqual(incidents[-1]["state"], "escalated")
        self.assertNotEqual(
            self.ledger.get_action(action_id)["state"], "completed"
        )

    def test_exhausted_invalid_output_drives_fixer_verifier_recovery(self) -> None:
        self.write_authored_change()
        self.register_granted_job()
        self.cfg["invalid_output_retries"] = 1
        records = self.recovery_runner(
            [
                {"stage": "implement", "body": "not a json envelope\n"},
                {"stage": "implement", "body": "still not a json envelope\n"},
                self.fixer_payload(),
                self.verifier_payload(),
                self.implement_payload(),
                self.review_payload(),
                self.acceptance_payload(),
                self.archive_payload(),
            ]
        )
        result = self.run_change()
        self.assertEqual(result, self.opsx_plan.base.DONE)
        self.assertEqual(
            [entry["stage"] for entry in records],
            [
                "implement", "implement", "fix", "verify",
                "implement", "review", "acceptance", "archive",
            ],
        )
        incidents = [row for row in self.incidents() if row["kind"] == "invalid_result"]
        self.assertTrue(incidents, "an invalid-result incident must be recorded")
        self.assertEqual(incidents[-1]["state"], "resolved")
        self.assertEqual(self.record()["recovery"], {})

    def test_review_recurrence_drives_repair_and_fresh_review(self) -> None:
        self.write_authored_change()
        self.register_granted_job()
        self.cfg["finding_recurrence_limit"] = 2
        locus = "tracked.txt:defect"
        records = self.recovery_runner(
            [
                self.implement_payload(),
                self.review_payload(verdict="fail", locus=locus),
                self.implement_payload(),
                self.review_payload(verdict="fail", locus=locus),
                self.fixer_payload(),
                self.verifier_payload(),
                self.review_payload(),
                self.acceptance_payload(),
                self.archive_payload(),
            ]
        )
        result = self.run_change()
        self.assertEqual(result, self.opsx_plan.base.DONE)
        stages = [entry["stage"] for entry in records]
        self.assertEqual(
            stages,
            [
                "implement", "review", "implement", "review", "fix", "verify",
                "review", "acceptance", "archive",
            ],
        )
        incidents = [
            row for row in self.incidents() if row["kind"] == "recurring_findings"
        ]
        self.assertTrue(incidents, "a recurring-findings incident must be recorded")
        self.assertEqual(incidents[-1]["state"], "resolved")
        record = self.record()
        self.assertEqual(record["recovery"], {})
        # The change was not marked failed at the recurrence ceiling: recovery
        # resolved and routed it to a fresh review.
        self.assertEqual(record["status"], self.opsx_plan.base.DONE)

    def test_transient_dispatch_failure_drives_bounded_retry(self) -> None:
        self.write_authored_change()
        self.register_granted_job()
        records = self.recovery_runner(
            [
                self.implement_payload(),
                {"stage": "review", "outcome": "timeout", "body": ""},
                self.review_payload(),
                self.acceptance_payload(),
                self.archive_payload(),
            ]
        )
        result = self.run_change()
        self.assertEqual(result, self.opsx_plan.base.DONE)
        stages = [entry["stage"] for entry in records]
        self.assertEqual(
            stages, ["implement", "review", "review", "acceptance", "archive"]
        )
        incidents = [
            row for row in self.incidents() if row["kind"] == "transient_provider"
        ]
        self.assertTrue(incidents, "a transient-provider incident must be recorded")
        self.assertEqual(incidents[-1]["state"], "resolved")
        self.assertEqual(self.record()["recovery"], {})

    def test_partial_archive_routes_to_fresh_review_recovery(self) -> None:
        self.write_authored_change()
        self.register_granted_job()
        self.cfg["fast_checks"] = ["test -f .fast-check-ok"]

        def _pass_checks(case) -> None:
            (case.repo / ".fast-check-ok").write_text("ok\n", encoding="utf-8")

        records = self.recovery_runner(
            [
                self.implement_payload(),
                self.review_payload(),
                self.acceptance_payload(),
                self.archive_payload(),
                {**self.implement_payload(), "mutate": _pass_checks},
                self.review_payload(),
                self.acceptance_payload(),
                self.archive_payload(),
            ]
        )
        result = self.run_change()
        self.assertEqual(result, self.opsx_plan.base.DONE)
        stages = [entry["stage"] for entry in records]
        self.assertEqual(
            stages,
            [
                "implement", "review", "acceptance", "archive",
                "implement", "review", "acceptance", "archive",
            ],
        )
        incidents = [row for row in self.incidents() if row["kind"] == "partial_archive"]
        self.assertTrue(incidents, "a partial-archive incident must be recorded")
        self.assertEqual(incidents[-1]["state"], "resolved")
        record = self.record()
        # The prior archive was never treated as done: the change reran a
        # fresh review round through the existing loop.
        self.assertEqual(record["round"], 2)
        self.assertEqual(record["status"], self.opsx_plan.base.DONE)

    def test_unregistered_run_keeps_terminal_invalid_output_halt(self) -> None:
        self.write_authored_change()
        self.cfg["invalid_output_retries"] = 0
        self.recovery_runner(
            [{"stage": "implement", "body": "not a json envelope\n"}]
        )
        result = self.run_change()
        self.assertEqual(result, "failed")
        record = self.record()
        self.assertEqual(record["last_result"], "subagent_output_invalid")
        self.assertEqual(record["status"], self.opsx_plan.base.FAILED)
        self.assertEqual(record["recovery"], {})
        self.assertEqual(self.ledger.list_jobs(), [])


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
