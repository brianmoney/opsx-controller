"""Tests for the additive telemetry ``role`` dimension and core metrics.

Covers task groups 7 and 8 of ``add-supervision-budgets``:

- the additive, optional ``role`` field (legacy records without it stay valid,
  existing consumers unaffected);
- role population for supervised stages including create, retry, and
  escalation;
- core-metrics collection before report/dashboard aggregation, read-only;
- supervisor-family exclusion from the legacy leaderboard input only, leaving
  non-supervisor changes identical to the pre-change computation.
"""

from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from lib.metrics import aggregator
from lib.metrics.aggregator import (
    SUPERVISOR_FAMILY_ROLES,
    aggregate,
    collect_core_metrics,
    filter_leaderboard_records,
    is_supervisor_family_role,
)
from lib.orchestrator import telemetry as telemetry_mod

from tests.lib.metrics.test_aggregator import (
    _make_telemetry_record,
    _setup_fixture_dir,
)


def _record(role=None, **overrides):
    record = _make_telemetry_record(**overrides)
    if role is not None:
        record["role"] = role
    return record


class TelemetryRoleFieldTests(unittest.TestCase):
    def _build(self, **overrides):
        params = {
            "plan_name": "p",
            "run_id": "r",
            "change_id": "c",
            "stage": "implement",
            "round_num": 1,
            "status": "completed",
            "started_at": "2025-01-01T00:00:00+00:00",
            "ended_at": "2025-01-01T00:01:00+00:00",
            "duration_ms": 60000,
            "adapter": "opencode",
            "worker_command": "cmd",
            "timeout_seconds": 60,
        }
        params.update(overrides)
        return telemetry_mod.build_telemetry_record(**params)

    def test_role_absent_by_default_and_legacy_shape_preserved(self):
        record = self._build()
        self.assertNotIn("role", record)
        # Every legacy field is present with the same name/type.
        for key in ("schema_version", "uid", "plan_name", "run_id", "change_id",
                    "stage", "round", "status", "started_at", "ended_at",
                    "duration_ms", "invocation", "model", "result", "usage",
                    "cost"):
            self.assertIn(key, record)
        self.assertEqual(record["schema_version"],
                         telemetry_mod.TELEMETRY_SCHEMA_VERSION)

    def test_role_is_additive_when_supplied(self):
        record = self._build(role="supervised_author")
        self.assertEqual(record["role"], "supervised_author")
        # Legacy fields unchanged by the additive field.
        self.assertEqual(record["stage"], "implement")

    def test_stage_role_mapping_including_escalation(self):
        resolve = telemetry_mod.resolve_stage_role
        self.assertEqual(resolve("create"), "supervised_author")
        self.assertEqual(resolve("implement"), "implementer")
        self.assertEqual(resolve("review"), "reviewer")
        self.assertEqual(resolve("archive"), "archiver")
        self.assertIsNone(resolve("legacy-unknown-stage"))
        self.assertEqual(
            resolve("implement", escalation_active=True),
            "implementer_escalation",
        )

    def test_legacy_record_without_role_still_parses(self):
        record = self._build()
        line = json.dumps(record)
        parsed = json.loads(line)
        self.assertNotIn("role", parsed)
        # A consumer that only reads legacy fields is unaffected.
        self.assertEqual(parsed["stage"], "implement")


class SupervisorFamilyFilterTests(unittest.TestCase):
    def test_supervisor_family_roles_identified(self):
        for role in SUPERVISOR_FAMILY_ROLES:
            self.assertTrue(is_supervisor_family_role(role))
        self.assertFalse(is_supervisor_family_role("implementer"))
        self.assertFalse(is_supervisor_family_role(None))

    def test_filter_removes_supervisor_family_only(self):
        records = [
            {"stage": "implement", "role": "implementer"},
            {"stage": "create", "role": "supervised_author"},
            {"stage": "review", "role": "reviewer"},
            {"stage": "legacy", "role": "fixer"},
            {"stage": "legacy-no-role"},
        ]
        filtered = filter_leaderboard_records(records)
        stages = [r["stage"] for r in filtered]
        self.assertEqual(stages, ["implement", "review", "legacy-no-role"])


class CoreMetricsCollectionTests(unittest.TestCase):
    def test_collects_per_role_and_totals_read_only(self):
        records = [
            _record(stage="implement", role="implementer", total_tokens=100,
                    estimated_cost=0.10, duration_ms=1000),
            _record(stage="implement", role="implementer", total_tokens=50,
                    estimated_cost=0.05, duration_ms=500),
            _record(stage="create", role="supervised_author", total_tokens=20,
                    estimated_cost=0.02, duration_ms=200),
        ]
        before = json.dumps(records, sort_keys=True)
        core, warnings = collect_core_metrics(records)
        after = json.dumps(records, sort_keys=True)
        self.assertEqual(before, after)  # read-only
        self.assertEqual(core.total_tokens, 170)
        self.assertAlmostEqual(core.total_estimated_cost, 0.17)
        self.assertEqual(core.total_duration_ms, 1700)
        implementer = core.role("implementer")
        self.assertIsNotNone(implementer)
        self.assertEqual(implementer.tokens, 150)
        self.assertAlmostEqual(implementer.estimated_cost, 0.15)
        self.assertIsNotNone(core.role("supervised_author"))

    def test_absent_ledger_is_not_a_warning(self):
        core, warnings = collect_core_metrics(
            [_record(role="implementer")], repo_root=Path("/nonexistent-repo-xyz")
        )
        self.assertEqual(warnings, [])
        self.assertEqual(core.reservation_count, 0)

    def test_reads_reservation_totals_from_real_ledger_read_only(self):
        import os
        from lib.supervisor import ledger, model_policy

        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        root = Path(tmp.name)
        repo = root / "repo"
        repo.mkdir()
        (repo / ".git").mkdir()
        worktree = repo / "wt"
        worktree.mkdir()
        storage = root / "storage"
        storage.mkdir()
        db_path = storage / "supervisor.sqlite3"

        handle = ledger.open_ledger(db_path, repository_root=repo)
        job_id = handle.register_job(
            run_id="run-1", worktree=worktree, owner="service", operator="operator",
            policy={
                "authority_config": {"mode": "policy-bound"},
                "model_selection": {
                    "version": model_policy.MODEL_POLICY_VERSION,
                    "roles": {"implementer": "cheap/x"},
                    "stages": {"implement": "implementer"},
                },
                "inexpensive_allowlist": {
                    "version": model_policy.MODEL_POLICY_VERSION,
                    "models": ["cheap/x"], "source": "test",
                },
                "manifest_snapshot_hash": "x",
                "budgets": {
                    "version": 1, "total_cost_usd": 100.0,
                    "per_action_cost_usd": None, "total_elapsed_minutes": None,
                    "per_action_elapsed_minutes": None, "max_incident_attempts": None,
                },
                "deadlines": {"version": 1, "execution_deadline_minutes": None},
            },
        )
        action_id = handle.begin_action(job_id, kind="implement", run_id="run-1")
        handle.dispatch_action(action_id)
        handle.insert_reservation(
            job_id, action_id=action_id, role="implementer",
            requested_model="cheap/x", reserved_cost_usd=2.5,
            reserved_elapsed_minutes=4.0,
        )
        handle.close()

        patcher = mock.patch.dict(
            os.environ, {"OPSX_SUPERVISOR_STATE_FILE": str(db_path)}
        )
        patcher.start()
        self.addCleanup(patcher.stop)

        before = db_path.read_bytes()
        core, warnings = collect_core_metrics([], repo_root=repo)
        self.assertEqual(warnings, [])
        self.assertEqual(core.reservation_count, 1)
        self.assertAlmostEqual(core.reserved_cost_usd, 2.5)
        self.assertAlmostEqual(core.reserved_elapsed_minutes, 4.0)
        # Read-only: the ledger file is byte-identical after collection.
        self.assertEqual(before, db_path.read_bytes())


class AggregateIntegrationTests(unittest.TestCase):
    def test_aggregate_collects_core_metrics_and_filters_leaderboard(self):
        with tempfile.TemporaryDirectory() as tmp:
            records = [
                _record(
                    change_id="ch-1", stage="implement", role="implementer",
                    model_id="m1", estimated_cost=0.20, verdict=None,
                ),
                _record(
                    change_id="ch-1", stage="review", role="reviewer",
                    model_id="m2", estimated_cost=0.08, verdict="pass",
                ),
                _record(
                    change_id="ch-1", stage="archive", role="archiver",
                    model_id="m3", estimated_cost=0.03,
                ),
                _record(
                    change_id="ch-1", stage="create", role="supervised_author",
                    model_id="super-model", estimated_cost=0.50,
                ),
                _record(
                    change_id="ch-1", stage="acceptance",
                    role="acceptance_reviewer", model_id="aux-model",
                    estimated_cost=0.30,
                ),
            ]
            repo = _setup_fixture_dir(
                tmp, telemetry_records=records,
                state={
                    "plan": "test-plan", "approvals": [],
                    "changes": {
                        "ch-1": {"status": "done", "round": 1, "phase": "done"}
                    },
                },
            )
            result = aggregate(repo, "test-plan")
            # Core metrics were collected and carried on the result.
            self.assertIsNotNone(result.core_metrics)
            self.assertIsNotNone(result.core_metrics.role("supervised_author"))
            self.assertIsNotNone(result.core_metrics.role("implementer"))
            # Supervisor-family usage does not pollute the leaderboard.
            for entry in result.model_leaderboard:
                for model in (entry.implementer_model, entry.reviewer_model,
                              entry.archiver_model):
                    self.assertNotIn(model, ("super-model", "aux-model"))

    def test_roleless_changes_leaderboard_identical(self):
        with tempfile.TemporaryDirectory() as tmp:
            records = [
                _record(change_id="ch-2", stage="implement", model_id="m1",
                        estimated_cost=0.2),
                _record(change_id="ch-2", stage="review", model_id="m2",
                        estimated_cost=0.1, verdict="pass"),
                _record(change_id="ch-2", stage="archive", model_id="m3",
                        estimated_cost=0.05),
            ]
            repo = _setup_fixture_dir(
                tmp, telemetry_records=records,
                state={
                    "plan": "test-plan", "approvals": [],
                    "changes": {
                        "ch-2": {"status": "done", "round": 1, "phase": "done"}
                    },
                },
            )
            result = aggregate(repo, "test-plan")
            direct = aggregator._build_leaderboard(
                result.change_metrics,
                aggregator.filter_leaderboard_records(records),
            )
            expected = aggregator._build_leaderboard(
                result.change_metrics, records
            )
            # Filtering is a no-op for role-less records.
            self.assertEqual(
                [vars(e) for e in direct],
                [vars(e) for e in expected],
            )


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
