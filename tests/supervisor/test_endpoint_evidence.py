"""Focused tests for worker-endpoint evidence classification and reconciliation.

The worker ``record_evidence`` verb appends evidence through the ledger and
drives the explicit reconcile-then-terminal lifecycle only for decisive worker
evidence. Non-decisive evidence (usage, session binding, unknown kinds,
unconfirmed payloads, or a confirmed payload with no explicit terminal result)
must leave an uncertain action uncertain. A reported ``session_id`` is bound to
the action's dispatch row before any evidence is accepted, and a binding
failure is surfaced rather than swallowed.
"""

from __future__ import annotations

import os
import tempfile
import unittest
from pathlib import Path

from lib.supervisor import broker as broker_module
from lib.supervisor import endpoints, ledger


def _policy() -> dict:
    return {
        "authority_config": {"mode": "policy-bound", "approval": "supervisor"},
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
        "manifest_snapshot_hash": "deadbeef",
        "budgets": {
            "version": 1,
            "total_cost_usd": 100.0,
            "per_action_cost_usd": None,
            "total_elapsed_minutes": None,
            "per_action_elapsed_minutes": None,
            "max_incident_attempts": None,
        },
        "deadlines": {"version": 1, "execution_deadline_minutes": None},
    }


class EndpointEvidenceTests(unittest.TestCase):
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
        self.ledger = ledger.open_ledger(
            self.storage / "supervisor.sqlite3", repository_root=self.repo
        )
        self.addCleanup(self.ledger.close)
        self.job_id = self.ledger.register_job(
            run_id="run-1",
            worktree=self.worktree,
            owner="service",
            policy=_policy(),
            operator="operator",
            manifest_content="[[changes]]\nid = \"change-a\"\n",
        )
        self.credentials = endpoints.PeerCredentials(
            pid=os.getpid(), uid=os.getuid(), gid=os.getgid()
        )

    def _uncertain_action(self) -> int:
        action_id = self.ledger.begin_action(
            self.job_id, kind="implement", run_id="run-1"
        )
        self.ledger.dispatch_action(action_id)
        self.ledger.mark_uncertain(action_id, detail="no confirmation")
        return action_id

    def _record(
        self,
        action_id: int,
        *,
        kind: str | None = "stage_result",
        payload: object = None,
        session_id: str | None = None,
    ) -> dict:
        request: dict = {
            "ledger": self.ledger,
            "job_id": self.job_id,
            "action_id": action_id,
        }
        if kind is not None:
            request["evidence"] = {"kind": kind, "payload": payload}
        if session_id is not None:
            request["session_id"] = session_id
        return endpoints._worker_record_evidence(request, self.credentials)

    def test_confirmed_terminal_stage_result_reconciles_to_completed(self) -> None:
        for payload in (
            {"confirmed": True, "outcome": "completed"},
            {"confirmed": True, "outcome": "exited"},
            {"confirmed": True, "outcome": "done"},
            {"confirmed": True, "completed": True},
        ):
            with self.subTest(payload=payload):
                action_id = self._uncertain_action()
                result = self._record(
                    action_id, kind="stage_result", payload=payload
                )
                self.assertEqual(result["state"], "completed")
                self.assertEqual(
                    self.ledger.get_action(action_id)["state"], "completed"
                )

    def test_confirmed_failure_reconciles_to_failed(self) -> None:
        for kind, payload in (
            ("stage_result", {"confirmed": True, "outcome": "failed"}),
            ("stage_result", {"confirmed": True, "completed": False}),
            ("spawn_loss", {"confirmed": True}),
        ):
            with self.subTest(kind=kind, payload=payload):
                action_id = self._uncertain_action()
                result = self._record(action_id, kind=kind, payload=payload)
                self.assertEqual(result["state"], "failed")
                self.assertEqual(
                    self.ledger.get_action(action_id)["state"], "failed"
                )

    def test_non_decisive_evidence_leaves_action_uncertain(self) -> None:
        cases = (
            ("usage", {"confirmed": True, "outcome": "completed"}),
            ("session_binding", {"confirmed": True, "outcome": "completed"}),
            ("unknown", {"confirmed": True, "outcome": "completed"}),
            ("stage_result", {"confirmed": False, "outcome": "completed"}),
            ("stage_result", {"confirmed": True}),
            ("stage_result", {"confirmed": True, "outcome": "perhaps"}),
            ("spawn_loss", {"confirmed": False}),
        )
        for kind, payload in cases:
            with self.subTest(kind=kind, payload=payload):
                action_id = self._uncertain_action()
                result = self._record(action_id, kind=kind, payload=payload)
                self.assertEqual(result["state"], "uncertain")
                self.assertEqual(
                    self.ledger.get_action(action_id)["state"], "uncertain"
                )

    def test_decisive_evidence_does_not_finalize_a_dispatched_action(self) -> None:
        action_id = self.ledger.begin_action(
            self.job_id, kind="implement", run_id="run-1"
        )
        self.ledger.dispatch_action(action_id)
        result = self._record(
            action_id,
            kind="stage_result",
            payload={"confirmed": True, "outcome": "completed"},
        )
        # Only an uncertain action is reconciled and finalized by the endpoint.
        self.assertEqual(result["state"], "dispatched")
        self.assertEqual(self.ledger.get_action(action_id)["state"], "dispatched")

    def test_session_id_binds_before_evidence_and_does_not_reconcile(self) -> None:
        action_id = self._uncertain_action()
        result = self._record(action_id, kind=None, session_id="task-7")
        self.assertEqual(result["session_id"], "task-7")
        row = self.ledger.latest_dispatch(action_id)
        self.assertEqual(row["session_id"], "task-7")
        kinds = [entry["kind"] for entry in self.ledger.list_evidence(action_id)]
        self.assertIn("session_binding", kinds)
        self.assertEqual(result["state"], "uncertain")
        self.assertEqual(self.ledger.get_action(action_id)["state"], "uncertain")

    def test_missing_dispatch_row_binding_error_is_not_swallowed(self) -> None:
        action_id = self.ledger.begin_action(
            self.job_id, kind="implement", run_id="run-1"
        )
        with self.assertRaises(ledger.UnknownRecordError):
            self._record(action_id, kind=None, session_id="task-9")
        # Binding precedes evidence acceptance, so nothing was journaled.
        self.assertEqual(self.ledger.list_evidence(action_id), [])

    def test_evidence_for_another_job_is_refused(self) -> None:
        other = self.root / "other"
        other.mkdir()
        (other / ".git").mkdir()
        other_worktree = other / "worktree"
        other_worktree.mkdir()
        job2 = self.ledger.register_job(
            run_id="run-2",
            worktree=other_worktree,
            owner="service",
            policy=_policy(),
            operator="operator",
            manifest_content="[[changes]]\nid = \"change-a\"\n",
        )
        foreign_action = self.ledger.begin_action(
            job2, kind="implement", run_id="run-2"
        )
        request = {
            "ledger": self.ledger,
            "job_id": self.job_id,
            "action_id": foreign_action,
            "evidence": {"kind": "stage_result", "payload": {"confirmed": True}},
        }
        with self.assertRaises(broker_module.BrokerMediationError):
            endpoints._worker_record_evidence(request, self.credentials)
        self.assertEqual(self.ledger.list_evidence(foreign_action), [])


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
