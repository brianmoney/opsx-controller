"""Tests for metrics aggregation.

Covers plan-level, change-level, stage-level, model leaderboard, cost
handling, and error/edge-case scenarios.
"""

import json
import os
import tempfile
import unittest
from pathlib import Path

from lib.metrics import (
    AggregationResult,
    ChangeMetrics,
    ModelLeaderboardEntry,
    PlanMetrics,
    StageAggregates,
    aggregate,
)


# ---------------------------------------------------------------------------
# Fixture helpers
# ---------------------------------------------------------------------------


def _make_telemetry_record(
    *,
    uid="t1",
    run_id="run1",
    change_id="change-a",
    stage="implement",
    round_num=1,
    status="completed",
    started_at="2025-01-01T00:00:00Z",
    ended_at="2025-01-01T00:01:00Z",
    duration_ms=60000,
    provider="openai",
    model_id="gpt-4o",
    total_tokens=5000,
    cost_status="estimated",
    estimated_cost=0.05,
    verdict=None,
):
    return {
        "schema_version": 1,
        "uid": uid,
        "plan_name": "test-plan",
        "run_id": run_id,
        "change_id": change_id,
        "stage": stage,
        "round": round_num,
        "status": status,
        "started_at": started_at,
        "ended_at": ended_at,
        "duration_ms": duration_ms,
        "invocation": {
            "adapter": "opencode",
            "worker_command": "test",
            "args_sample": None,
            "timeout_seconds": 600,
            "retry_attempt": 0,
        },
        "model": {
            "provider": provider,
            "model_id": model_id,
            "model_alias": None,
        },
        "result": {
            "log_path": "test.log",
            "stage_status": status,
            "error_message": None,
            "verdict": verdict,
            "critical_count": None,
            "warning_count": None,
            "note_count": None,
        },
        "usage": {
            "usage_available": True,
            "input_tokens": 3000,
            "output_tokens": 2000,
            "cached_input_tokens": None,
            "reasoning_tokens": None,
            "total_tokens": total_tokens,
            "usage_source": "test",
        },
        "cost": {
            "status": cost_status,
            "pricing_catalog_version": "1.0",
            "price_snapshot": {},
            "unresolved_reason": None,
            "estimated_cost": estimated_cost,
        },
    }


def _make_state(*, plan_name="test-plan", changes=None):
    if changes is None:
        changes = {}
    return {
        "plan": plan_name,
        "approvals": [],
        "changes": changes,
    }


def _make_change_record(
    status="pending", phase="implement", round_num=1, max_rounds=5
):
    return {
        "status": status,
        "phase": phase,
        "round": round_num,
        "max_rounds": max_rounds,
        "no_progress_streak": 0,
        "latest_fix_prompt": "",
        "task_counts": {"complete": 0, "total": 10},
        "tracked_change_files": [],
        "history": [],
    }


def _setup_fixture_dir(
    tmpdir, plan_name="test-plan", telemetry_records=None, state=None,
    create_empty_telemetry=False,
):
    """Create a repo root with telemetry and state for testing."""
    repo = Path(tmpdir) / "repo"
    opsx = repo / ".opsx-plan"
    telemetry_dir = opsx / "telemetry"
    telemetry_dir.mkdir(parents=True, exist_ok=True)

    if telemetry_records is not None:
        jsonl_path = telemetry_dir / f"{plan_name}.jsonl"
        with open(jsonl_path, "w", encoding="utf-8") as fh:
            for r in telemetry_records:
                fh.write(json.dumps(r) + "\n")
    elif create_empty_telemetry:
        jsonl_path = telemetry_dir / f"{plan_name}.jsonl"
        jsonl_path.write_text("", encoding="utf-8")

    if state:
        state_path = opsx / f"{plan_name}.state.json"
        state_path.parent.mkdir(parents=True, exist_ok=True)
        with open(state_path, "w", encoding="utf-8") as fh:
            json.dump(state, fh)

    return repo


# ---------------------------------------------------------------------------
# Tests: Data Types
# ---------------------------------------------------------------------------


class DataTypeTests(unittest.TestCase):
    """Verify dataclass construction and defaults."""

    def test_plan_metrics_defaults(self):
        pm = PlanMetrics(plan_name="test")
        self.assertEqual(pm.plan_name, "test")
        self.assertEqual(pm.run_id, "")
        self.assertIsNone(pm.completion_rate)
        self.assertIsNone(pm.success_rate)
        self.assertIsNone(pm.total_duration_ms)
        self.assertIsNone(pm.total_tokens)
        self.assertIsNone(pm.total_estimated_cost)
        self.assertEqual(pm.estimated_cost_changes, 0)
        self.assertEqual(pm.unresolved_cost_changes, 0)
        self.assertEqual(pm.unknown_cost_changes, 0)

    def test_change_metrics_defaults(self):
        cm = ChangeMetrics(change_id="test")
        self.assertEqual(cm.change_id, "test")
        self.assertEqual(cm.status, "incomplete")
        self.assertIsNone(cm.duration_ms)
        self.assertIsNone(cm.tokens)
        self.assertIsNone(cm.estimated_cost)
        self.assertEqual(cm.cost_status, "unavailable")
        self.assertIsNone(cm.first_pass_review)
        self.assertFalse(cm.no_progress)

    def test_stage_aggregates_defaults(self):
        sa = StageAggregates()
        self.assertIsNone(sa.average_rounds)
        self.assertIsNone(sa.median_rounds)
        self.assertIsNone(sa.average_cost_per_change)

    def test_model_leaderboard_entry_defaults(self):
        e = ModelLeaderboardEntry()
        self.assertIsNone(e.implementer_model)
        self.assertIsNone(e.average_cost)

    def test_aggregation_result_defaults(self):
        ar = AggregationResult()
        self.assertIsInstance(ar.plan_metrics, PlanMetrics)
        self.assertEqual(ar.plan_metrics.plan_name, "")
        self.assertEqual(ar.change_metrics, [])
        self.assertIsInstance(ar.stage_aggregates, StageAggregates)
        self.assertEqual(ar.model_leaderboard, [])
        self.assertEqual(ar.warnings, [])


# ---------------------------------------------------------------------------
# Tests: Plan-level aggregation
# ---------------------------------------------------------------------------


class PlanLevelTests(unittest.TestCase):
    """Test plan-level aggregation scenarios."""

    def test_successful_2_change_run(self):
        """9.1: Plan-level aggregation for a successful 2-change run."""
        records = [
            _make_telemetry_record(
                uid="1", change_id="change-a", stage="implement",
                estimated_cost=0.10,
            ),
            _make_telemetry_record(
                uid="2", change_id="change-a", stage="review",
                estimated_cost=0.05, verdict="pass",
            ),
            _make_telemetry_record(
                uid="3", change_id="change-a", stage="archive",
                estimated_cost=0.02,
            ),
            _make_telemetry_record(
                uid="4", change_id="change-b", stage="implement",
                estimated_cost=0.08,
            ),
            _make_telemetry_record(
                uid="5", change_id="change-b", stage="review",
                estimated_cost=0.04, verdict="pass",
            ),
            _make_telemetry_record(
                uid="6", change_id="change-b", stage="archive",
                estimated_cost=0.02,
            ),
        ]
        state = _make_state(
            changes={
                "change-a": _make_change_record(status="done"),
                "change-b": _make_change_record(status="done"),
            }
        )
        repo = _setup_fixture_dir(
            tempfile.mkdtemp(), telemetry_records=records, state=state
        )

        result = aggregate(str(repo), "test-plan")

        self.assertEqual(result.plan_metrics.total_changes, 2)
        self.assertEqual(result.plan_metrics.completed_changes, 2)
        self.assertEqual(result.plan_metrics.failed_changes, 0)
        self.assertEqual(result.plan_metrics.blocked_changes, 0)
        self.assertEqual(result.plan_metrics.incomplete_changes, 0)
        self.assertEqual(result.plan_metrics.completion_rate, 1.0)
        self.assertEqual(result.plan_metrics.success_rate, 1.0)
        self.assertEqual(result.plan_metrics.estimated_cost_changes, 2)
        self.assertEqual(result.plan_metrics.unresolved_cost_changes, 0)
        # Total estimated cost = 0.10+0.05+0.02+0.08+0.04+0.02 = 0.31
        self.assertAlmostEqual(result.plan_metrics.total_estimated_cost, 0.31)

    def test_partially_complete_run(self):
        """Plan with mixed status changes."""
        records = [
            _make_telemetry_record(
                uid="1", change_id="change-a", estimated_cost=0.10,
            ),
            _make_telemetry_record(
                uid="2", change_id="change-b", estimated_cost=0.05,
            ),
        ]
        state = _make_state(
            changes={
                "change-a": _make_change_record(status="done"),
                "change-b": _make_change_record(status="failed"),
                "change-c": _make_change_record(status="pending"),
            }
        )
        repo = _setup_fixture_dir(
            tempfile.mkdtemp(), telemetry_records=records, state=state
        )

        result = aggregate(str(repo), "test-plan")

        self.assertEqual(result.plan_metrics.total_changes, 3)
        self.assertEqual(result.plan_metrics.completed_changes, 1)
        self.assertEqual(result.plan_metrics.failed_changes, 1)
        self.assertEqual(result.plan_metrics.incomplete_changes, 1)
        self.assertAlmostEqual(result.plan_metrics.completion_rate, 1 / 3)
        self.assertAlmostEqual(result.plan_metrics.success_rate, 0.5)

    def test_blocked_change_in_plan(self):
        """Plan with blocked change."""
        state = _make_state(
            changes={
                "change-a": _make_change_record(status="done"),
                "change-b": _make_change_record(status="blocked"),
            }
        )
        repo = _setup_fixture_dir(
            tempfile.mkdtemp(), telemetry_records=[], state=state
        )

        result = aggregate(str(repo), "test-plan")
        self.assertEqual(result.plan_metrics.blocked_changes, 1)
        self.assertEqual(result.plan_metrics.incomplete_changes, 0)

    def test_incomplete_plan(self):
        """9.6: Incomplete plan (some changes not started)."""
        state = _make_state(
            changes={
                "change-a": _make_change_record(status="done"),
                "change-b": _make_change_record(status="pending"),
                "change-c": _make_change_record(status="pending"),
            }
        )
        repo = _setup_fixture_dir(
            tempfile.mkdtemp(), telemetry_records=[], state=state
        )

        result = aggregate(str(repo), "test-plan")
        self.assertEqual(result.plan_metrics.completed_changes, 1)
        self.assertEqual(result.plan_metrics.incomplete_changes, 2)
        self.assertAlmostEqual(result.plan_metrics.completion_rate, 1 / 3)

    def test_total_duration_and_tokens(self):
        """Plan metrics sum durations and tokens correctly."""
        records = [
            _make_telemetry_record(
                uid="1", change_id="a", duration_ms=30000, total_tokens=1000,
            ),
            _make_telemetry_record(
                uid="2", change_id="a", duration_ms=20000, total_tokens=800,
            ),
        ]
        state = _make_state(
            changes={"a": _make_change_record(status="done")}
        )
        repo = _setup_fixture_dir(
            tempfile.mkdtemp(), telemetry_records=records, state=state
        )

        result = aggregate(str(repo), "test-plan")
        self.assertEqual(result.plan_metrics.total_duration_ms, 50000)
        self.assertEqual(result.plan_metrics.total_tokens, 1800)


# ---------------------------------------------------------------------------
# Tests: Change-level aggregation
# ---------------------------------------------------------------------------


class ChangeLevelTests(unittest.TestCase):
    """Test per-change aggregation scenarios."""

    def test_completed_change_single_round(self):
        """9.2: Completed change with one round (first-pass review pass)."""
        records = [
            _make_telemetry_record(
                uid="1", change_id="change-a", stage="implement", round_num=1,
                status="completed",
            ),
            _make_telemetry_record(
                uid="2", change_id="change-a", stage="review", round_num=1,
                verdict="pass", status="completed",
            ),
            _make_telemetry_record(
                uid="3", change_id="change-a", stage="archive", round_num=1,
                status="completed",
            ),
        ]
        state = _make_state(
            changes={"change-a": _make_change_record(status="done", round_num=1)}
        )
        repo = _setup_fixture_dir(
            tempfile.mkdtemp(), telemetry_records=records, state=state
        )

        result = aggregate(str(repo), "test-plan")
        self.assertEqual(len(result.change_metrics), 1)
        cm = result.change_metrics[0]
        self.assertEqual(cm.change_id, "change-a")
        self.assertEqual(cm.status, "completed")
        self.assertEqual(cm.total_rounds, 1)
        self.assertTrue(cm.first_pass_review)
        self.assertEqual(cm.review_failures, 0)
        self.assertFalse(cm.no_progress)
        self.assertFalse(cm.max_rounds_exceeded)
        self.assertFalse(cm.archive_failed)

    def test_multi_round_change(self):
        """9.3: Change that required 3 rounds before passing."""
        records = [
            _make_telemetry_record(
                uid="1", change_id="change-a", stage="implement", round_num=1,
            ),
            _make_telemetry_record(
                uid="2", change_id="change-a", stage="review", round_num=1,
                verdict="fail",
            ),
            _make_telemetry_record(
                uid="3", change_id="change-a", stage="implement", round_num=2,
            ),
            _make_telemetry_record(
                uid="4", change_id="change-a", stage="review", round_num=2,
                verdict="fail",
            ),
            _make_telemetry_record(
                uid="5", change_id="change-a", stage="implement", round_num=3,
            ),
            _make_telemetry_record(
                uid="6", change_id="change-a", stage="review", round_num=3,
                verdict="pass",
            ),
            _make_telemetry_record(
                uid="7", change_id="change-a", stage="archive", round_num=3,
            ),
        ]
        state = _make_state(
            changes={"change-a": _make_change_record(status="done", round_num=3)}
        )
        repo = _setup_fixture_dir(
            tempfile.mkdtemp(), telemetry_records=records, state=state
        )

        result = aggregate(str(repo), "test-plan")
        cm = result.change_metrics[0]
        self.assertEqual(cm.status, "completed")
        self.assertEqual(cm.total_rounds, 3)
        self.assertFalse(cm.first_pass_review)
        self.assertEqual(cm.review_failures, 2)

    def test_failed_change_max_rounds(self):
        """9.4: Failed change with max rounds exceeded."""
        records = [
            _make_telemetry_record(
                uid="1", change_id="change-a", stage="implement", round_num=1,
            ),
            _make_telemetry_record(
                uid="2", change_id="change-a", stage="review", round_num=1,
                verdict="fail",
            ),
            _make_telemetry_record(
                uid="3", change_id="change-a", stage="implement", round_num=2,
            ),
            _make_telemetry_record(
                uid="4", change_id="change-a", stage="review", round_num=2,
                verdict="fail",
            ),
            _make_telemetry_record(
                uid="5", change_id="change-a", stage="implement", round_num=3,
            ),
            _make_telemetry_record(
                uid="6", change_id="change-a", stage="review", round_num=3,
                verdict="fail",
            ),
        ]
        state = _make_state(
            changes={
                "change-a": _make_change_record(
                    status="failed", round_num=3, max_rounds=3
                )
            }
        )
        repo = _setup_fixture_dir(
            tempfile.mkdtemp(), telemetry_records=records, state=state
        )

        result = aggregate(str(repo), "test-plan")
        cm = result.change_metrics[0]
        self.assertEqual(cm.status, "failed")
        self.assertEqual(cm.total_rounds, 3)
        self.assertTrue(cm.max_rounds_exceeded)
        self.assertEqual(cm.review_failures, 3)

    def test_blocked_change(self):
        """9.5: Blocked change."""
        state = _make_state(
            changes={
                "change-a": _make_change_record(status="blocked", round_num=2)
            }
        )
        repo = _setup_fixture_dir(
            tempfile.mkdtemp(), telemetry_records=[], state=state
        )

        result = aggregate(str(repo), "test-plan")
        cm = result.change_metrics[0]
        self.assertEqual(cm.status, "blocked")
        self.assertEqual(cm.total_rounds, 2)

    def test_no_progress_detection(self):
        """Detect no-progress from state."""
        state = _make_state(
            changes={
                "change-a": _make_change_record(status="failed", round_num=3),
            }
        )
        state["changes"]["change-a"]["no_progress_streak"] = 2
        repo = _setup_fixture_dir(
            tempfile.mkdtemp(), telemetry_records=[], state=state
        )

        result = aggregate(str(repo), "test-plan")
        cm = result.change_metrics[0]
        self.assertTrue(cm.no_progress)

    def test_archive_failed_detection(self):
        """Archive stage non-completed."""
        records = [
            _make_telemetry_record(
                uid="1", change_id="change-a", stage="implement", round_num=1,
            ),
            _make_telemetry_record(
                uid="2", change_id="change-a", stage="review", round_num=1,
                verdict="pass",
            ),
            _make_telemetry_record(
                uid="3", change_id="change-a", stage="archive", round_num=1,
                status="error",
            ),
        ]
        state = _make_state(
            changes={"change-a": _make_change_record(status="failed")}
        )
        repo = _setup_fixture_dir(
            tempfile.mkdtemp(), telemetry_records=records, state=state
        )

        result = aggregate(str(repo), "test-plan")
        cm = result.change_metrics[0]
        self.assertTrue(cm.archive_failed)

    def test_archived_stage_status_is_completed(self):
        """Direct archiver telemetry records successful archives as archived."""
        archive = _make_telemetry_record(
            uid="3", change_id="change-a", stage="archive", round_num=1,
        )
        archive["result"]["stage_status"] = "archived"
        records = [
            _make_telemetry_record(
                uid="1", change_id="change-a", stage="implement", round_num=1,
            ),
            _make_telemetry_record(
                uid="2", change_id="change-a", stage="review", round_num=1,
                verdict="pass",
            ),
            archive,
        ]
        state = _make_state(
            changes={"change-a": _make_change_record(status="done")}
        )
        repo = _setup_fixture_dir(
            tempfile.mkdtemp(), telemetry_records=records, state=state
        )

        result = aggregate(str(repo), "test-plan")
        cm = result.change_metrics[0]
        self.assertEqual(cm.status, "completed")
        self.assertFalse(cm.archive_failed)

    def test_fast_check_failed(self):
        """First-round implement unsuccessful."""
        records = [
            _make_telemetry_record(
                uid="1", change_id="change-a", stage="implement", round_num=1,
                status="error",
            ),
        ]
        state = _make_state(
            changes={"change-a": _make_change_record(status="failed")}
        )
        repo = _setup_fixture_dir(
            tempfile.mkdtemp(), telemetry_records=records, state=state
        )

        result = aggregate(str(repo), "test-plan")
        cm = result.change_metrics[0]
        self.assertTrue(cm.fast_check_failed)


# ---------------------------------------------------------------------------
# Tests: Stage-level aggregates
# ---------------------------------------------------------------------------


class StageAggregateTests(unittest.TestCase):
    """Test stage-level aggregate statistics."""

    def test_average_and_median_rounds(self):
        """6.1: average_rounds and median_rounds."""
        records = [
            _make_telemetry_record(uid="1", change_id="a", round_num=1),
            _make_telemetry_record(uid="2", change_id="b", round_num=3),
        ]
        state = _make_state(
            changes={
                "a": _make_change_record(status="done", round_num=1),
                "b": _make_change_record(status="done", round_num=3),
            }
        )
        repo = _setup_fixture_dir(
            tempfile.mkdtemp(), telemetry_records=records, state=state
        )

        result = aggregate(str(repo), "test-plan")
        self.assertEqual(result.stage_aggregates.average_rounds, 2.0)
        self.assertEqual(result.stage_aggregates.median_rounds, 2.0)

    def test_stage_duration_averages(self):
        """6.2: Average duration per stage type."""
        records = [
            _make_telemetry_record(
                uid="1", change_id="a", stage="implement", duration_ms=30000,
            ),
            _make_telemetry_record(
                uid="2", change_id="a", stage="implement", duration_ms=45000,
            ),
            _make_telemetry_record(
                uid="3", change_id="a", stage="review", duration_ms=10000,
            ),
            _make_telemetry_record(
                uid="4", change_id="a", stage="archive", duration_ms=5000,
            ),
        ]
        state = _make_state(
            changes={"a": _make_change_record(status="done")}
        )
        repo = _setup_fixture_dir(
            tempfile.mkdtemp(), telemetry_records=records, state=state
        )

        result = aggregate(str(repo), "test-plan")
        self.assertEqual(
            result.stage_aggregates.average_duration_implement, 37500.0
        )
        self.assertEqual(
            result.stage_aggregates.average_duration_review, 10000.0
        )
        self.assertEqual(
            result.stage_aggregates.average_duration_archive, 5000.0
        )

    def test_review_failure_rate(self):
        """6.3: review_failure_rate."""
        records = [
            _make_telemetry_record(
                uid="1", change_id="a", stage="review", round_num=1,
                verdict="fail",
            ),
            _make_telemetry_record(
                uid="2", change_id="a", stage="review", round_num=2,
                verdict="pass",
            ),
        ]
        state = _make_state(
            changes={"a": _make_change_record(status="done", round_num=2)}
        )
        repo = _setup_fixture_dir(
            tempfile.mkdtemp(), telemetry_records=records, state=state
        )

        result = aggregate(str(repo), "test-plan")
        self.assertEqual(result.stage_aggregates.review_failure_rate, 0.5)

    def test_average_tokens_and_cost_per_change(self):
        """6.4: average per change using only changes with estimated cost."""
        records = [
            _make_telemetry_record(
                uid="1", change_id="a", total_tokens=5000, estimated_cost=0.10,
            ),
            _make_telemetry_record(
                uid="2", change_id="b", total_tokens=3000, estimated_cost=0.05,
            ),
        ]
        state = _make_state(
            changes={
                "a": _make_change_record(status="done"),
                "b": _make_change_record(status="done"),
            }
        )
        repo = _setup_fixture_dir(
            tempfile.mkdtemp(), telemetry_records=records, state=state
        )

        result = aggregate(str(repo), "test-plan")
        self.assertEqual(
            result.stage_aggregates.average_tokens_per_change, 4000.0
        )
        self.assertAlmostEqual(
            result.stage_aggregates.average_cost_per_change, 0.075
        )

    def test_rounds_none_when_no_completed_changes(self):
        """Averages are None when no completed changes."""
        state = _make_state(
            changes={"a": _make_change_record(status="pending")}
        )
        repo = _setup_fixture_dir(
            tempfile.mkdtemp(), telemetry_records=[], state=state
        )

        result = aggregate(str(repo), "test-plan")
        self.assertIsNone(result.stage_aggregates.average_rounds)
        self.assertIsNone(result.stage_aggregates.median_rounds)
        self.assertIsNone(result.stage_aggregates.average_cost_per_change)


# ---------------------------------------------------------------------------
# Tests: Cost handling
# ---------------------------------------------------------------------------


class CostHandlingTests(unittest.TestCase):
    """Test cost separation and null handling."""

    def test_mixed_estimated_and_unresolved(self):
        """9.7: Mixed estimated, unresolved, and unavailable costs."""
        records = [
            _make_telemetry_record(
                uid="1", change_id="a", cost_status="estimated",
                estimated_cost=1.00,
            ),
            _make_telemetry_record(
                uid="2", change_id="b", cost_status="estimated",
                estimated_cost=2.00,
            ),
            _make_telemetry_record(
                uid="3", change_id="c", cost_status="unresolved",
                estimated_cost=None,
            ),
        ]
        state = _make_state(
            changes={
                "a": _make_change_record(status="done"),
                "b": _make_change_record(status="done"),
                "c": _make_change_record(status="done"),
            }
        )
        repo = _setup_fixture_dir(
            tempfile.mkdtemp(), telemetry_records=records, state=state
        )

        result = aggregate(str(repo), "test-plan")
        # Unresolved-only changes (not also estimated)
        # change c has unresolved, but a and b have estimated
        self.assertEqual(result.plan_metrics.estimated_cost_changes, 2)
        self.assertEqual(result.plan_metrics.unresolved_cost_changes, 1)
        self.assertAlmostEqual(result.plan_metrics.total_estimated_cost, 3.0)

        # Average cost should be 1.5 (only a and b)
        self.assertAlmostEqual(
            result.stage_aggregates.average_cost_per_change, 1.5
        )

    def test_all_costs_unresolved(self):
        """All costs unresolved: averages are None."""
        records = [
            _make_telemetry_record(
                uid="1", change_id="a", cost_status="unresolved",
                estimated_cost=None,
            ),
        ]
        state = _make_state(
            changes={"a": _make_change_record(status="done")}
        )
        repo = _setup_fixture_dir(
            tempfile.mkdtemp(), telemetry_records=records, state=state
        )

        result = aggregate(str(repo), "test-plan")
        self.assertIsNone(result.plan_metrics.total_estimated_cost)
        self.assertIsNone(result.stage_aggregates.average_cost_per_change)
        self.assertEqual(result.plan_metrics.estimated_cost_changes, 0)

    def test_zero_cost_is_included(self):
        """8.3: Zero estimated cost is included in sums and averages."""
        records = [
            _make_telemetry_record(
                uid="1", change_id="a", cost_status="estimated",
                estimated_cost=0.0,
            ),
            _make_telemetry_record(
                uid="2", change_id="b", cost_status="estimated",
                estimated_cost=2.00,
            ),
        ]
        state = _make_state(
            changes={
                "a": _make_change_record(status="done"),
                "b": _make_change_record(status="done"),
            }
        )
        repo = _setup_fixture_dir(
            tempfile.mkdtemp(), telemetry_records=records, state=state
        )

        result = aggregate(str(repo), "test-plan")
        self.assertEqual(result.plan_metrics.estimated_cost_changes, 2)
        self.assertAlmostEqual(result.plan_metrics.total_estimated_cost, 2.0)
        self.assertAlmostEqual(
            result.stage_aggregates.average_cost_per_change, 1.0
        )

    def test_cost_status_partial(self):
        """8.2: unresolved costs don't contribute to sums."""
        records = [
            _make_telemetry_record(
                uid="1", change_id="a", cost_status="estimated",
                estimated_cost=1.00,
            ),
            _make_telemetry_record(
                uid="2", change_id="a", cost_status="unresolved",
                estimated_cost=None,
            ),
        ]
        state = _make_state(
            changes={"a": _make_change_record(status="done")}
        )
        repo = _setup_fixture_dir(
            tempfile.mkdtemp(), telemetry_records=records, state=state
        )

        result = aggregate(str(repo), "test-plan")
        cm = result.change_metrics[0]
        self.assertEqual(cm.cost_status, "partial")
        self.assertAlmostEqual(cm.estimated_cost, 1.00)

    def test_divide_by_zero_safe(self):
        """8.4: Never divide by zero; average is None."""
        # No state and no telemetry -- zero changes means rates are None
        repo = _setup_fixture_dir(tempfile.mkdtemp())
        result = aggregate(str(repo), "test-plan")

        self.assertIsNone(result.plan_metrics.completion_rate)
        self.assertIsNone(result.plan_metrics.success_rate)
        self.assertIsNone(result.stage_aggregates.average_cost_per_change)


# ---------------------------------------------------------------------------
# Tests: Model leaderboard
# ---------------------------------------------------------------------------


class ModelLeaderboardTests(unittest.TestCase):
    """Test model-combination leaderboard entries."""

    def test_leaderboard_with_fully_identified_models(self):
        """9.8: Leaderboard with fully identified models."""
        records = [
            _make_telemetry_record(
                uid="1", change_id="a", stage="implement", round_num=1,
                provider="openai", model_id="gpt-4o",
            ),
            _make_telemetry_record(
                uid="2", change_id="a", stage="review", round_num=1,
                provider="anthropic", model_id="claude-sonnet", verdict="pass",
            ),
            _make_telemetry_record(
                uid="3", change_id="a", stage="archive", round_num=1,
                provider="openai", model_id="gpt-4o-mini",
            ),
            _make_telemetry_record(
                uid="4", change_id="b", stage="implement", round_num=1,
                provider="openai", model_id="gpt-4o",
            ),
            _make_telemetry_record(
                uid="5", change_id="b", stage="review", round_num=1,
                provider="anthropic", model_id="claude-sonnet", verdict="pass",
            ),
            _make_telemetry_record(
                uid="6", change_id="b", stage="archive", round_num=1,
                provider="openai", model_id="gpt-4o-mini",
            ),
        ]
        state = _make_state(
            changes={
                "a": _make_change_record(status="done", round_num=1),
                "b": _make_change_record(status="done", round_num=1),
            }
        )
        repo = _setup_fixture_dir(
            tempfile.mkdtemp(), telemetry_records=records, state=state
        )

        result = aggregate(str(repo), "test-plan")

        # Should have per-role entries + one full combination entry
        self.assertGreater(len(result.model_leaderboard), 0)

        # Find full-combination entry
        full_entries = [
            e for e in result.model_leaderboard
            if (e.implementer_model and e.reviewer_model and e.archiver_model)
        ]
        self.assertEqual(len(full_entries), 1)
        fe = full_entries[0]
        self.assertEqual(fe.implementer_model, "openai:gpt-4o")
        self.assertEqual(fe.reviewer_model, "anthropic:claude-sonnet")
        self.assertEqual(fe.archiver_model, "openai:gpt-4o-mini")
        self.assertEqual(fe.change_count, 2)
        self.assertEqual(fe.success_rate, 1.0)
        self.assertEqual(fe.first_pass_rate, 1.0)

    def test_leaderboard_partial_unknown_model(self):
        """9.9: Partially unknown model identity."""
        records = [
            _make_telemetry_record(
                uid="1", change_id="a", stage="implement", round_num=1,
                provider="openai", model_id="gpt-4o",
            ),
            _make_telemetry_record(
                uid="2", change_id="a", stage="review", round_num=1,
                provider=None, model_id=None, verdict="pass",
            ),
            _make_telemetry_record(
                uid="3", change_id="a", stage="archive", round_num=1,
                provider=None, model_id=None,
            ),
        ]
        state = _make_state(
            changes={"a": _make_change_record(status="done", round_num=1)}
        )
        repo = _setup_fixture_dir(
            tempfile.mkdtemp(), telemetry_records=records, state=state
        )

        result = aggregate(str(repo), "test-plan")

        # Full combination should have no entries (archiver unknown)
        full_entries = [
            e for e in result.model_leaderboard
            if (e.implementer_model and e.reviewer_model and e.archiver_model)
        ]
        self.assertEqual(len(full_entries), 0)

        # But implementer should be present
        impl_entries = [
            e for e in result.model_leaderboard
            if e.implementer_model == "openai:gpt-4o"
        ]
        self.assertEqual(len(impl_entries), 1)
        self.assertEqual(impl_entries[0].change_count, 1)

    def test_leaderboard_with_no_records(self):
        """Leaderboard is empty when no records."""
        state = _make_state(
            changes={"a": _make_change_record(status="done")}
        )
        repo = _setup_fixture_dir(
            tempfile.mkdtemp(), telemetry_records=[], state=state
        )

        result = aggregate(str(repo), "test-plan")
        self.assertEqual(len(result.model_leaderboard), 0)


# ---------------------------------------------------------------------------
# Tests: Telemetry file handling
# ---------------------------------------------------------------------------


class TelemetryHandlingTests(unittest.TestCase):
    """Test telemetry file reading and run selection."""

    def test_missing_telemetry_file(self):
        """9.11: Missing telemetry file returns empty result with warning."""
        repo = _setup_fixture_dir(tempfile.mkdtemp())
        result = aggregate(str(repo), "test-plan")

        self.assertEqual(result.change_metrics, [])
        self.assertTrue(
            any("not found" in w for w in result.warnings)
        )

    def test_missing_state_file(self):
        """9.12: Missing state file derives from telemetry."""
        records = [
            _make_telemetry_record(uid="1", change_id="a"),
        ]
        repo = _setup_fixture_dir(
            tempfile.mkdtemp(), telemetry_records=records, state=None
        )

        result = aggregate(str(repo), "test-plan")
        self.assertTrue(
            any("state file" in w.lower() for w in result.warnings)
        )
        self.assertGreater(len(result.change_metrics), 0)

    def test_empty_telemetry_file(self):
        """Empty telemetry with state returns incomplete changes."""
        state = _make_state(
            changes={"a": _make_change_record(status="pending")}
        )
        repo = _setup_fixture_dir(
            tempfile.mkdtemp(), telemetry_records=None,
            create_empty_telemetry=True, state=state,
        )

        result = aggregate(str(repo), "test-plan")
        self.assertTrue(
            any("zero records" in w for w in result.warnings)
        )
        self.assertEqual(result.change_metrics[0].status, "incomplete")

    def test_multi_run_telemetry_selects_latest(self):
        """9.10: Multi-run telemetry with run_id filtering."""
        records = [
            _make_telemetry_record(
                uid="1", change_id="a", run_id="run1",
                started_at="2025-01-01T00:00:00Z", estimated_cost=0.10,
            ),
            _make_telemetry_record(
                uid="2", change_id="a", run_id="run2",
                started_at="2025-01-02T00:00:00Z", estimated_cost=0.50,
            ),
        ]
        state = _make_state(
            changes={"a": _make_change_record(status="done")}
        )
        repo = _setup_fixture_dir(
            tempfile.mkdtemp(), telemetry_records=records, state=state
        )

        result = aggregate(str(repo), "test-plan")
        # Latest run (run2) selected
        self.assertEqual(result.plan_metrics.run_id, "run2")
        self.assertAlmostEqual(result.plan_metrics.total_estimated_cost, 0.50)

    def test_explicit_run_id_selection(self):
        """Select a specific run_id."""
        records = [
            _make_telemetry_record(
                uid="1", change_id="a", run_id="run1",
                started_at="2025-01-01T00:00:00Z", estimated_cost=0.10,
            ),
            _make_telemetry_record(
                uid="2", change_id="a", run_id="run2",
                started_at="2025-01-02T00:00:00Z", estimated_cost=0.50,
            ),
        ]
        state = _make_state(
            changes={"a": _make_change_record(status="done")}
        )
        repo = _setup_fixture_dir(
            tempfile.mkdtemp(), telemetry_records=records, state=state
        )

        result = aggregate(str(repo), "test-plan", run_id="run1")
        self.assertEqual(result.plan_metrics.run_id, "run1")
        self.assertAlmostEqual(result.plan_metrics.total_estimated_cost, 0.10)

    def test_unknown_run_id_warns(self):
        """Requesting a non-existent run_id produces a warning."""
        records = [
            _make_telemetry_record(uid="1", change_id="a", run_id="run1"),
        ]
        repo = _setup_fixture_dir(
            tempfile.mkdtemp(), telemetry_records=records, state=None
        )

        result = aggregate(str(repo), "test-plan", run_id="nonexistent")
        self.assertTrue(
            any("not found" in w for w in result.warnings)
        )

    def test_schema_version_handling(self):
        """Records with different schema_version handled without failure."""
        r1 = _make_telemetry_record(uid="1", change_id="a")
        r1["schema_version"] = 2
        r2 = _make_telemetry_record(uid="2", change_id="a")
        r2["schema_version"] = 1
        state = _make_state(
            changes={"a": _make_change_record(status="done")}
        )
        repo = _setup_fixture_dir(
            tempfile.mkdtemp(), telemetry_records=[r1, r2], state=state
        )

        result = aggregate(str(repo), "test-plan")
        self.assertEqual(len(result.change_metrics), 1)


# ---------------------------------------------------------------------------
# Tests: Repo validation
# ---------------------------------------------------------------------------


class RepoValidationTests(unittest.TestCase):
    """Test repo_root validation."""

    def test_invalid_repo_root_raises(self):
        """Non-existent repo_root raises AggregationError."""
        from lib.metrics import AggregationError
        with self.assertRaises(AggregationError):
            aggregate("/nonexistent/path/12345", "test")


# ---------------------------------------------------------------------------
# Regression tests: spec-required behaviours fixed in round 2
# ---------------------------------------------------------------------------


class RegressionTests(unittest.TestCase):
    """Regression tests for review findings."""

    def test_plan_duration_only_completed_stages(self):
        """Plan total_duration_ms only sums completed stage records."""
        records = [
            _make_telemetry_record(
                uid="1", change_id="a", stage="implement",
                status="completed", duration_ms=10000,
            ),
            _make_telemetry_record(
                uid="2", change_id="a", stage="review",
                status="error", duration_ms=99999,
            ),
        ]
        state = _make_state(
            changes={"a": _make_change_record(status="done")}
        )
        repo = _setup_fixture_dir(
            tempfile.mkdtemp(), telemetry_records=records, state=state
        )
        result = aggregate(str(repo), "test-plan")
        self.assertEqual(result.plan_metrics.total_duration_ms, 10000)

    def test_stage_durations_only_completed_records(self):
        """Stage duration averages only use completed stage records."""
        records = [
            _make_telemetry_record(
                uid="1", change_id="a", stage="implement",
                status="completed", duration_ms=10000,
            ),
            _make_telemetry_record(
                uid="2", change_id="a", stage="implement",
                status="error", duration_ms=99999,
            ),
            _make_telemetry_record(
                uid="3", change_id="a", stage="review",
                status="completed", duration_ms=5000,
            ),
            _make_telemetry_record(
                uid="4", change_id="a", stage="archive",
                status="completed", duration_ms=3000,
            ),
        ]
        state = _make_state(
            changes={"a": _make_change_record(status="done")}
        )
        repo = _setup_fixture_dir(
            tempfile.mkdtemp(), telemetry_records=records, state=state
        )
        result = aggregate(str(repo), "test-plan")
        self.assertEqual(
            result.stage_aggregates.average_duration_implement, 10000.0
        )
        self.assertEqual(
            result.stage_aggregates.average_duration_review, 5000.0
        )
        self.assertEqual(
            result.stage_aggregates.average_duration_archive, 3000.0
        )

    def test_warning_unknown_schema_version(self):
        """Warning emitted for records with schema_version != 1."""
        r = _make_telemetry_record(uid="1", change_id="a")
        r["schema_version"] = 99
        state = _make_state(
            changes={"a": _make_change_record(status="done")}
        )
        repo = _setup_fixture_dir(
            tempfile.mkdtemp(), telemetry_records=[r], state=state
        )
        result = aggregate(str(repo), "test-plan")
        self.assertTrue(
            any("schema_version 99" in w for w in result.warnings),
            f"Expected schema_version warning, got: {result.warnings}",
        )

    def test_warning_change_in_telemetry_not_in_state(self):
        """Warning when telemetry has a change not in plan state."""
        records = [
            _make_telemetry_record(uid="1", change_id="orphan"),
        ]
        repo = _setup_fixture_dir(
            tempfile.mkdtemp(), telemetry_records=records,
            state=_make_state(changes={}),
        )
        result = aggregate(str(repo), "test-plan")
        self.assertTrue(
            any(
                "not in plan state" in w and "orphan" in w
                for w in result.warnings
            ),
            f"Expected orphan-change warning, got: {result.warnings}",
        )

    def test_warning_change_in_state_no_telemetry(self):
        """Warning when state has a change with no telemetry records."""
        state = _make_state(
            changes={
                "a": _make_change_record(status="done"),
                "b": _make_change_record(status="pending"),
            }
        )
        records = [
            _make_telemetry_record(uid="1", change_id="a"),
        ]
        repo = _setup_fixture_dir(
            tempfile.mkdtemp(), telemetry_records=records, state=state
        )
        result = aggregate(str(repo), "test-plan")
        self.assertTrue(
            any(
                "has no telemetry records" in w and "b" in w
                for w in result.warnings
            ),
            f"Expected no-telemetry-for-b warning, got: {result.warnings}",
        )

    def test_warning_unresolved_cost_records(self):
        """Warning emitted for records with cost.status='unresolved'."""
        records = [
            _make_telemetry_record(
                uid="1", change_id="a", cost_status="unresolved",
                estimated_cost=None,
            ),
        ]
        state = _make_state(
            changes={"a": _make_change_record(status="done")}
        )
        repo = _setup_fixture_dir(
            tempfile.mkdtemp(), telemetry_records=records, state=state
        )
        result = aggregate(str(repo), "test-plan")
        self.assertTrue(
            any(
                "cost.status='unresolved'" in w
                for w in result.warnings
            ),
            f"Expected unresolved-cost warning, got: {result.warnings}",
        )

    def test_first_pass_review_requires_explicit_pass(self):
        """First-pass review is False when verdict is not explicitly 'pass'."""
        records = [
            _make_telemetry_record(
                uid="1", change_id="a", stage="implement", round_num=1,
                status="completed",
            ),
            _make_telemetry_record(
                uid="2", change_id="a", stage="review", round_num=1,
                verdict=None, status="completed",
            ),
            _make_telemetry_record(
                uid="3", change_id="a", stage="archive", round_num=1,
                status="completed",
            ),
        ]
        state = _make_state(
            changes={"a": _make_change_record(status="done", round_num=1)}
        )
        repo = _setup_fixture_dir(
            tempfile.mkdtemp(), telemetry_records=records, state=state
        )
        result = aggregate(str(repo), "test-plan")
        cm = result.change_metrics[0]
        self.assertFalse(
            cm.first_pass_review,
            "first_pass_review should be False when verdict is not explicit 'pass'",
        )

    def test_change_status_state_done_with_last_review_fail(self):
        """State done + last review fail -> status 'failed'."""
        records = [
            _make_telemetry_record(
                uid="1", change_id="a", stage="implement", round_num=1,
                status="completed",
            ),
            _make_telemetry_record(
                uid="2", change_id="a", stage="review", round_num=1,
                verdict="fail", status="completed",
            ),
        ]
        state = _make_state(
            changes={"a": _make_change_record(status="done", round_num=1)}
        )
        repo = _setup_fixture_dir(
            tempfile.mkdtemp(), telemetry_records=records, state=state
        )
        result = aggregate(str(repo), "test-plan")
        cm = result.change_metrics[0]
        self.assertEqual(cm.status, "failed")

    def test_change_status_state_done_with_archive_error(self):
        """State done + archive not completed -> status 'failed'."""
        records = [
            _make_telemetry_record(
                uid="1", change_id="a", stage="implement", round_num=1,
                status="completed",
            ),
            _make_telemetry_record(
                uid="2", change_id="a", stage="review", round_num=1,
                verdict="pass", status="completed",
            ),
            _make_telemetry_record(
                uid="3", change_id="a", stage="archive", round_num=1,
                status="error",
            ),
        ]
        state = _make_state(
            changes={"a": _make_change_record(status="done", round_num=1)}
        )
        repo = _setup_fixture_dir(
            tempfile.mkdtemp(), telemetry_records=records, state=state
        )
        result = aggregate(str(repo), "test-plan")
        cm = result.change_metrics[0]
        self.assertEqual(cm.status, "failed")

    def test_conflicting_status_warning(self):
        """Warning when state done but telemetry shows non-passing."""
        records = [
            _make_telemetry_record(
                uid="1", change_id="a", stage="review", round_num=1,
                verdict="fail", status="completed",
            ),
        ]
        state = _make_state(
            changes={"a": _make_change_record(status="done")}
        )
        repo = _setup_fixture_dir(
            tempfile.mkdtemp(), telemetry_records=records, state=state
        )
        result = aggregate(str(repo), "test-plan")
        self.assertTrue(
            any(
                "non-passing outcomes" in w
                for w in result.warnings
            ),
            f"Expected non-passing warning, got: {result.warnings}",
        )

    def test_state_done_with_no_telemetry_is_completed(self):
        """State done with no telemetry records -> status 'completed'."""
        state = _make_state(
            changes={"a": _make_change_record(status="done")}
        )
        repo = _setup_fixture_dir(
            tempfile.mkdtemp(), telemetry_records=[], state=state
        )
        result = aggregate(str(repo), "test-plan")
        cm = result.change_metrics[0]
        self.assertEqual(cm.status, "completed")


# ---------------------------------------------------------------------------
# Regression tests: review-finding fixes (round 3)
# ---------------------------------------------------------------------------


class Round3RegressionTests(unittest.TestCase):
    """Regression tests for round-3 review findings."""

    # -- Fix 1: plan counts follow change-level state+telemetry status --

    def test_plan_counts_follow_change_status(self):
        """Plan completed/failed counts use derived change status, not raw state.

        State says 'done' but last review verdict is 'fail', so the
        change-level status is 'failed'.  Plan counts must reflect this.
        """
        records = [
            _make_telemetry_record(
                uid="1", change_id="a", stage="implement", round_num=1,
                status="completed",
            ),
            _make_telemetry_record(
                uid="2", change_id="a", stage="review", round_num=1,
                verdict="fail", status="completed",
            ),
            _make_telemetry_record(
                uid="3", change_id="b", stage="implement", round_num=1,
                status="completed",
            ),
            _make_telemetry_record(
                uid="4", change_id="b", stage="review", round_num=1,
                verdict="pass", status="completed",
            ),
        ]
        state = _make_state(
            changes={
                "a": _make_change_record(status="done"),
                "b": _make_change_record(status="done"),
            }
        )
        repo = _setup_fixture_dir(
            tempfile.mkdtemp(), telemetry_records=records, state=state
        )
        result = aggregate(str(repo), "test-plan")
        self.assertEqual(result.plan_metrics.total_changes, 2)
        # change a: done but last review fail → status failed
        # change b: done, review pass → status completed
        self.assertEqual(result.plan_metrics.completed_changes, 1)
        self.assertEqual(result.plan_metrics.failed_changes, 1)
        self.assertAlmostEqual(result.plan_metrics.success_rate, 0.5)

    def test_plan_counts_failed_when_archive_not_completed(self):
        """State 'done' + archive not completed → plan counts as failed."""
        records = [
            _make_telemetry_record(
                uid="1", change_id="a", stage="implement", round_num=1,
                status="completed",
            ),
            _make_telemetry_record(
                uid="2", change_id="a", stage="review", round_num=1,
                verdict="pass", status="completed",
            ),
            _make_telemetry_record(
                uid="3", change_id="a", stage="archive", round_num=1,
                status="error",
            ),
        ]
        state = _make_state(
            changes={"a": _make_change_record(status="done")}
        )
        repo = _setup_fixture_dir(
            tempfile.mkdtemp(), telemetry_records=records, state=state
        )
        result = aggregate(str(repo), "test-plan")
        self.assertEqual(result.plan_metrics.failed_changes, 1)
        self.assertEqual(result.plan_metrics.completed_changes, 0)

    # -- Fix 2: warning when telemetry shows completion but state not done --

    def test_warning_telemetry_completed_state_not_done(self):
        """Warning when telemetry has a completed archive but state is pending."""
        records = [
            _make_telemetry_record(
                uid="1", change_id="a", stage="implement", round_num=1,
                status="completed",
            ),
            _make_telemetry_record(
                uid="2", change_id="a", stage="review", round_num=1,
                verdict="pass", status="completed",
            ),
            _make_telemetry_record(
                uid="3", change_id="a", stage="archive", round_num=1,
                status="completed",
            ),
        ]
        state = _make_state(
            changes={"a": _make_change_record(status="pending")}
        )
        repo = _setup_fixture_dir(
            tempfile.mkdtemp(), telemetry_records=records, state=state
        )
        result = aggregate(str(repo), "test-plan")
        self.assertTrue(
            any(
                "completed archive" in w
                and "does not mark it done" in w
                for w in result.warnings
            ),
            f"Expected telemetry-completed-but-state-not-done warning, "
            f"got: {result.warnings}",
        )

    def test_no_warning_when_state_and_telemetry_agree_done(self):
        """No warning when state done and telemetry archive completed."""
        records = [
            _make_telemetry_record(
                uid="1", change_id="a", stage="implement", round_num=1,
                status="completed",
            ),
            _make_telemetry_record(
                uid="2", change_id="a", stage="review", round_num=1,
                verdict="pass", status="completed",
            ),
            _make_telemetry_record(
                uid="3", change_id="a", stage="archive", round_num=1,
                status="completed",
            ),
        ]
        state = _make_state(
            changes={"a": _make_change_record(status="done")}
        )
        repo = _setup_fixture_dir(
            tempfile.mkdtemp(), telemetry_records=records, state=state
        )
        result = aggregate(str(repo), "test-plan")
        self.assertFalse(
            any(
                "completed archive" in w
                for w in result.warnings
            ),
            f"Unexpected warning about archive/state mismatch: "
            f"{result.warnings}",
        )

    # -- Fix 3: review_failure_rate from all review stages --

    def test_review_failure_rate_counts_failed_changes(self):
        """review_failure_rate includes review failures from all changes.

        Even a failed change's review failures must be counted.
        """
        records = [
            # change a (completed): 1 fail, 1 pass
            _make_telemetry_record(
                uid="1", change_id="a", stage="implement", round_num=1,
                status="completed",
            ),
            _make_telemetry_record(
                uid="2", change_id="a", stage="review", round_num=1,
                verdict="fail", status="completed",
            ),
            _make_telemetry_record(
                uid="3", change_id="a", stage="implement", round_num=2,
            ),
            _make_telemetry_record(
                uid="4", change_id="a", stage="review", round_num=2,
                verdict="pass", status="completed",
            ),
            # change b (failed): 1 fail
            _make_telemetry_record(
                uid="5", change_id="b", stage="implement", round_num=1,
                status="completed",
            ),
            _make_telemetry_record(
                uid="6", change_id="b", stage="review", round_num=1,
                verdict="fail", status="completed",
            ),
        ]
        state = _make_state(
            changes={
                "a": _make_change_record(status="done", round_num=2),
                "b": _make_change_record(status="failed", round_num=1),
            }
        )
        repo = _setup_fixture_dir(
            tempfile.mkdtemp(), telemetry_records=records, state=state
        )
        result = aggregate(str(repo), "test-plan")
        # 3 review stages total: 2 fails + 1 pass = 2/3
        self.assertAlmostEqual(
            result.stage_aggregates.review_failure_rate, 2.0 / 3.0
        )

    # -- Fix 4: partial-cost changes counted in unresolved_cost_changes --

    def test_partial_cost_change_counted_in_unresolved(self):
        """A change with both estimated and unresolved costs counts in both.

        Plan-level unresolved_cost_changes must include changes that also
        have estimated cost records (partial-cost changes).
        """
        records = [
            _make_telemetry_record(
                uid="1", change_id="a", cost_status="estimated",
                estimated_cost=1.00,
            ),
            _make_telemetry_record(
                uid="2", change_id="a", cost_status="unresolved",
                estimated_cost=None,
            ),
            _make_telemetry_record(
                uid="3", change_id="b", cost_status="estimated",
                estimated_cost=2.00,
            ),
        ]
        state = _make_state(
            changes={
                "a": _make_change_record(status="done"),
                "b": _make_change_record(status="done"),
            }
        )
        repo = _setup_fixture_dir(
            tempfile.mkdtemp(), telemetry_records=records, state=state
        )
        result = aggregate(str(repo), "test-plan")
        # Both a and b have estimated cost records
        self.assertEqual(result.plan_metrics.estimated_cost_changes, 2)
        # Change a also has unresolved cost → counted in unresolved too
        self.assertEqual(result.plan_metrics.unresolved_cost_changes, 1)

    def test_pure_unresolved_change_still_counted(self):
        """A change with only unresolved cost is counted in unresolved_cost_changes."""
        records = [
            _make_telemetry_record(
                uid="1", change_id="a", cost_status="unresolved",
                estimated_cost=None,
            ),
        ]
        state = _make_state(
            changes={"a": _make_change_record(status="done")}
        )
        repo = _setup_fixture_dir(
            tempfile.mkdtemp(), telemetry_records=records, state=state
        )
        result = aggregate(str(repo), "test-plan")
        self.assertEqual(result.plan_metrics.estimated_cost_changes, 0)
        self.assertEqual(result.plan_metrics.unresolved_cost_changes, 1)


# ---------------------------------------------------------------------------
# Regression tests: review-finding fixes (round 4)
# ---------------------------------------------------------------------------


class Round4RegressionTests(unittest.TestCase):
    """Regression tests for round-4 review findings."""

    # -- Fix 1: no_progress true when any no-progress cycle is recorded --

    def test_no_progress_streak_one(self):
        """no_progress is True even with a single no-progress cycle."""
        state = _make_state(
            changes={
                "change-a": _make_change_record(status="failed", round_num=3),
            }
        )
        state["changes"]["change-a"]["no_progress_streak"] = 1
        repo = _setup_fixture_dir(
            tempfile.mkdtemp(), telemetry_records=[], state=state
        )
        result = aggregate(str(repo), "test-plan")
        cm = result.change_metrics[0]
        self.assertTrue(
            cm.no_progress,
            "no_progress should be True when no_progress_streak >= 1",
        )

    def test_no_progress_streak_zero(self):
        """no_progress is False when no_progress_streak is 0."""
        state = _make_state(
            changes={
                "change-a": _make_change_record(status="failed", round_num=3),
            }
        )
        state["changes"]["change-a"]["no_progress_streak"] = 0
        repo = _setup_fixture_dir(
            tempfile.mkdtemp(), telemetry_records=[], state=state
        )
        result = aggregate(str(repo), "test-plan")
        cm = result.change_metrics[0]
        self.assertFalse(cm.no_progress)

    # -- Fix 2: unavailable cost_status not counted in unresolved_cost_changes --

    def test_unavailable_cost_not_in_unresolved(self):
        """Plan-level unresolved_cost_changes excludes unavailable cost_status."""
        records = [
            _make_telemetry_record(
                uid="1", change_id="a", cost_status="unavailable",
                estimated_cost=None, provider=None, model_id=None,
            ),
            _make_telemetry_record(
                uid="2", change_id="b", cost_status="unresolved",
                estimated_cost=None,
            ),
            _make_telemetry_record(
                uid="3", change_id="c", cost_status="estimated",
                estimated_cost=1.00,
            ),
        ]
        state = _make_state(
            changes={
                "a": _make_change_record(status="done"),
                "b": _make_change_record(status="done"),
                "c": _make_change_record(status="done"),
            }
        )
        repo = _setup_fixture_dir(
            tempfile.mkdtemp(), telemetry_records=records, state=state
        )
        result = aggregate(str(repo), "test-plan")
        # Change a has unavailable cost → not in unresolved
        # Change b has unresolved → counted in unresolved
        # Change c has estimated → counted in estimated
        self.assertEqual(result.plan_metrics.estimated_cost_changes, 1)
        self.assertEqual(result.plan_metrics.unresolved_cost_changes, 1)
        # Change a: unavailable, known model → not in unknown; but model is None
        # so it is in unknown_cost_changes (model unknown)
        self.assertGreaterEqual(result.plan_metrics.unknown_cost_changes, 1)

    def test_change_cost_status_unavailable(self):
        """Change with only unavailable cost records has cost_status='unavailable'."""
        records = [
            _make_telemetry_record(
                uid="1", change_id="a", cost_status="unavailable",
                estimated_cost=None,
            ),
        ]
        state = _make_state(
            changes={"a": _make_change_record(status="done")}
        )
        repo = _setup_fixture_dir(
            tempfile.mkdtemp(), telemetry_records=records, state=state
        )
        result = aggregate(str(repo), "test-plan")
        cm = result.change_metrics[0]
        self.assertEqual(cm.cost_status, "unavailable")

    def test_change_cost_status_estimated_with_unavailable(self):
        """Estimated + unavailable → cost_status='estimated', not 'partial'."""
        records = [
            _make_telemetry_record(
                uid="1", change_id="a", cost_status="estimated",
                estimated_cost=1.00,
            ),
            _make_telemetry_record(
                uid="2", change_id="a", cost_status="unavailable",
                estimated_cost=None,
            ),
        ]
        state = _make_state(
            changes={"a": _make_change_record(status="done")}
        )
        repo = _setup_fixture_dir(
            tempfile.mkdtemp(), telemetry_records=records, state=state
        )
        result = aggregate(str(repo), "test-plan")
        cm = result.change_metrics[0]
        self.assertEqual(cm.cost_status, "estimated")

    # -- Fix 3: archive failures at round limit don't set max_rounds_exceeded --

    def test_archive_failure_at_limit_no_max_rounds(self):
        """Archive failure at round limit does NOT set max_rounds_exceeded."""
        records = [
            _make_telemetry_record(
                uid="1", change_id="change-a", stage="implement", round_num=1,
                status="completed",
            ),
            _make_telemetry_record(
                uid="2", change_id="change-a", stage="review", round_num=1,
                verdict="pass", status="completed",
            ),
            _make_telemetry_record(
                uid="3", change_id="change-a", stage="implement", round_num=2,
                status="completed",
            ),
            _make_telemetry_record(
                uid="4", change_id="change-a", stage="review", round_num=2,
                verdict="pass", status="completed",
            ),
            _make_telemetry_record(
                uid="5", change_id="change-a", stage="archive", round_num=2,
                status="error",
            ),
        ]
        state = _make_state(
            changes={
                "change-a": _make_change_record(
                    status="failed", round_num=2, max_rounds=2
                )
            }
        )
        repo = _setup_fixture_dir(
            tempfile.mkdtemp(), telemetry_records=records, state=state
        )
        result = aggregate(str(repo), "test-plan")
        cm = result.change_metrics[0]
        self.assertEqual(cm.status, "failed")
        self.assertEqual(cm.total_rounds, 2)
        self.assertTrue(cm.archive_failed, "archive_failed should be True")
        self.assertFalse(
            cm.max_rounds_exceeded,
            "max_rounds_exceeded should be False for archive failure at round limit",
        )

    def test_review_fail_at_limit_sets_max_rounds(self):
        """Review fail at round limit DOES set max_rounds_exceeded (no archive)."""
        records = [
            _make_telemetry_record(
                uid="1", change_id="change-a", stage="implement", round_num=1,
                status="completed",
            ),
            _make_telemetry_record(
                uid="2", change_id="change-a", stage="review", round_num=1,
                verdict="fail", status="completed",
            ),
            _make_telemetry_record(
                uid="3", change_id="change-a", stage="implement", round_num=2,
                status="completed",
            ),
            _make_telemetry_record(
                uid="4", change_id="change-a", stage="review", round_num=2,
                verdict="fail", status="completed",
            ),
        ]
        state = _make_state(
            changes={
                "change-a": _make_change_record(
                    status="failed", round_num=2, max_rounds=2
                )
            }
        )
        repo = _setup_fixture_dir(
            tempfile.mkdtemp(), telemetry_records=records, state=state
        )
        result = aggregate(str(repo), "test-plan")
        cm = result.change_metrics[0]
        self.assertEqual(cm.status, "failed")
        self.assertEqual(cm.total_rounds, 2)
        self.assertTrue(
            cm.max_rounds_exceeded,
            "max_rounds_exceeded should be True when review fails at round limit",
        )


if __name__ == "__main__":
    unittest.main()
