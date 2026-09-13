"""Orchestrator wiring tests for the supervised budget gate.

Exercises task group 3 (reserve-before-dispatch and reconcile-after-dispatch in
the run paths), group 4 (deadline accounting), group 5 (attempt signatures),
and group 6 (blocked states / operator-only changes) at the real ``run_direct_change``
call site, using a registered supervised job and a patched stage dispatcher.
"""

from __future__ import annotations

import importlib.util
import json
import os
import sqlite3
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from lib.supervisor import budgets, ledger, model_policy

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
            "implement_invoke": "opencode run --agent opsx-implementer",
            "review_invoke": "opencode run --agent opsx-reviewer",
            "archive_invoke": "opencode run --agent opsx-archiver",
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
        )
        return job_id

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
        # A second identical failure is refused before it is dispatched.
        invoked: list[str] = []
        self.opsx_plan.invoke_direct_stage = lambda *a, **k: invoked.append("called")
        self.opsx_plan.state_mod.set_status(
            self.state, self.cid, "pending", "retry"
        )
        self.opsx_plan.state_mod.rec(self.state, self.cid)["phase"] = "implement"
        result = self.run_change()
        self.assertEqual(result, "budget")
        self.assertEqual(invoked, [])

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


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
