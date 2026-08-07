"""Tests for lib.orchestrator.report (the `opsx-plan report` command).
Split out of test_opsx_plan.py during the orchestrator module extraction.
"""
from __future__ import annotations

import argparse
import io
import json
import os
import subprocess
import sys
import unittest
from pathlib import Path

import tempfile
from unittest import mock

from lib.models import resolver

_MODEL_HOME: tempfile.TemporaryDirectory | None = None
_MODEL_CONFIG_PATCH = None
_MODEL_ENV_PATCH = None


def setUpModule() -> None:
    """Pin model resolution so the suite does not read ambient configuration.

    Resolution consults ~/.config/opsx-controller/models.toml and then the
    OPSX_*_MODEL environment variables. Left unpinned, this suite passes on a
    machine that happens to have models configured and fails on a clean
    checkout or a CI runner, where stages cannot resolve a model and report
    `cannot activate models for adapter ...: unresolved role(s)`.
    """
    global _MODEL_HOME, _MODEL_CONFIG_PATCH, _MODEL_ENV_PATCH
    _MODEL_HOME = tempfile.TemporaryDirectory()
    _MODEL_CONFIG_PATCH = mock.patch.object(
        resolver, "USER_CONFIG_PATH", Path(_MODEL_HOME.name) / "models.toml"
    )
    _MODEL_CONFIG_PATCH.start()
    _MODEL_ENV_PATCH = mock.patch.dict(
        os.environ,
        {
            "OPSX_CONTROLLER_MODEL": "test-provider/test-controller",
            "OPSX_IMPLEMENTER_MODEL": "test-provider/test-implementer",
            "OPSX_REVIEWER_MODEL": "test-provider/test-reviewer",
            "OPSX_ARCHIVER_MODEL": "test-provider/test-archiver",
        },
    )
    _MODEL_ENV_PATCH.start()


def tearDownModule() -> None:
    assert _MODEL_ENV_PATCH is not None
    assert _MODEL_CONFIG_PATCH is not None
    assert _MODEL_HOME is not None
    _MODEL_ENV_PATCH.stop()
    _MODEL_CONFIG_PATCH.stop()
    _MODEL_HOME.cleanup()


def git(repo: Path, *args: str) -> None:
    subprocess.run(["git", *args], cwd=repo, check=True, capture_output=True, text=True)

from lib.orchestrator import report


class ReportCommandTests(unittest.TestCase):
    """Tests for ``opsx-plan report``: table output, JSON output, filters,
    edge cases, and idempotency (tasks 7.1–7.14)."""

    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.repo = Path(self.tmp.name)
        git(self.repo, "init")
        git(
            self.repo,
            "-c", "user.email=test@example.invalid",
            "-c", "user.name=Test User",
            "commit", "-m", "init", "--allow-empty",
        )

    def tearDown(self) -> None:
        self.tmp.cleanup()

    # -- telemetry / state / plan helpers -------------------------------------

    @staticmethod
    def _build_record(*, stage: str, change_id: str, run_id: str = "run-001",
                      plan_name: str = "test-plan", round_num: int = 1,
                      status: str = "completed",
                      duration_ms: int = 5000,
                      input_tokens: int = 10000,
                      output_tokens: int = 2000,
                      cost_status: str = "estimated",
                      estimated_cost: float = 0.05,
                      provider: str = "openai",
                      model_id: str = "gpt-4o",
                      verdict: str | None = None,
                      critical: int = 0, warning: int = 0, note: int = 0,
                      stage_status: str | None = None,
                      started_at: str = "2026-07-01T10:00:00Z",
                      ) -> dict:
        """Build a single telemetry JSONL record dict."""
        import uuid as _uuid_module
        rec: dict = {
            "uid": str(_uuid_module.uuid4()),
            "schema_version": 1,
            "plan_name": plan_name,
            "run_id": run_id,
            "change_id": change_id,
            "stage": stage,
            "round": round_num,
            "status": status,
            "started_at": started_at,
            "ended_at": "2026-07-01T10:00:05Z",
            "duration_ms": duration_ms,
            "usage": {
                "usage_available": True,
                "input_tokens": input_tokens,
                "output_tokens": output_tokens,
                "cached_input_tokens": None,
                "reasoning_tokens": None,
                "total_tokens": input_tokens + output_tokens if input_tokens is not None and output_tokens is not None else None,
                "usage_source": "worker_json",
            },
            "cost": {
                "status": cost_status,
                "estimated_cost": estimated_cost,
                "pricing_catalog_version": "1.0.0" if cost_status == "estimated" else None,
                "price_snapshot": None,
                "unresolved_reason": None if cost_status == "estimated" else "unresolved reason",
            },
            "model": {
                "provider": provider,
                "model_id": model_id,
                "model_alias": None,
            },
            "result": {
                "stage_status": stage_status or ("completed" if status == "completed" else None),
                "verdict": verdict,
                "critical_count": critical,
                "warning_count": warning,
                "note_count": note,
            },
        }
        return rec

    def _write_telemetry(self, plan_name: str, records: list[dict]) -> None:
        """Write records to .opsx-plan/telemetry/<plan_name>.jsonl."""
        tele_dir = self.repo / ".opsx-plan" / "telemetry"
        tele_dir.mkdir(parents=True, exist_ok=True)
        jsonl = tele_dir / f"{plan_name}.jsonl"
        lines = "\n".join(json.dumps(r, sort_keys=True, ensure_ascii=True)
                          for r in records) + "\n"
        jsonl.write_text(lines, encoding="utf-8")

    def _write_state(self, plan_name: str, state: dict) -> None:
        """Write plan state to .opsx-plan/<plan_name>.state.json."""
        state_dir = self.repo / ".opsx-plan"
        state_dir.mkdir(parents=True, exist_ok=True)
        p = state_dir / f"{plan_name}.state.json"
        p.write_text(json.dumps(state, sort_keys=True, ensure_ascii=True), encoding="utf-8")

    def _write_plan_toml(self, name: str = "test-plan", changes: list[str] | None = None) -> Path:
        """Write a minimal plan TOML and return its path."""
        if changes is None:
            changes = ["ch-single"]
        change_entries = "\n".join(
            f"[[changes]]\nid = \"{c}\"\n" for c in changes
        )
        content = (
            f'[plan]\nname = "{name}"\nadapter = "opencode"\n'
            f'timeout_minutes = 1\nmax_rounds = 5\n'
            f'require_clean_tracked = false\n'
            f"{change_entries}\n"
        )
        p = self.repo / f"{name}.toml"
        p.write_text(content, encoding="utf-8")
        return p

    def _report_args(self, plan_path: str | Path, **kw) -> argparse.Namespace:
        """Build an argparse.Namespace for cmd_report."""
        return argparse.Namespace(
            repo=str(self.repo),
            plan=str(plan_path),
            json=kw.get("json", False),
            change=kw.get("change"),
            run_id=kw.get("run_id"),
            stage=kw.get("stage"),
            model=kw.get("model"),
        )

    def _run_report(self, **kw) -> tuple[int, str, str]:
        """Run cmd_report, return (rc, stdout, stderr)."""
        plan_path = kw.pop("plan_path", self._write_plan_toml())
        import sys as _sys
        saved_path = list(_sys.path)
        try:
            args = self._report_args(plan_path, **kw)
            stdout = io.StringIO()
            stderr = io.StringIO()
            with mock.patch("sys.stdout", stdout), mock.patch("sys.stderr", stderr):
                rc = report.cmd_report(args)
            return rc, stdout.getvalue(), stderr.getvalue()
        finally:
            _sys.path[:] = saved_path

    # -- 7.1 table output for completed single-change plan with estimated costs

    def test_table_output_single_change_estimated_costs(self) -> None:
        plan_name = "single-plan"
        cid = "ch-single"
        plan_path = self._write_plan_toml(plan_name, [cid])

        records = [
            self._build_record(
                stage="implement", change_id=cid, plan_name=plan_name,
                duration_ms=120000, input_tokens=50000, output_tokens=10000,
                estimated_cost=0.25, cost_status="estimated",
            ),
            self._build_record(
                stage="review", change_id=cid, plan_name=plan_name,
                duration_ms=30000, input_tokens=20000, output_tokens=5000,
                estimated_cost=0.10, cost_status="estimated",
                verdict="pass",
            ),
            self._build_record(
                stage="archive", change_id=cid, plan_name=plan_name,
                duration_ms=10000, input_tokens=8000, output_tokens=2000,
                estimated_cost=0.04, cost_status="estimated",
            ),
        ]
        self._write_telemetry(plan_name, records)
        self._write_state(plan_name, {
            "plan": plan_name, "approvals": [],
            "changes": {
                cid: {"status": "done", "round": 1, "phase": "done"},
            },
        })

        rc, stdout, stderr = self._run_report(
            plan_path=plan_path, plan_name=plan_name,
        )
        self.assertEqual(rc, 0)
        # Plan summary
        self.assertIn("=== Plan Summary ===", stdout)
        self.assertIn(plan_name, stdout)
        self.assertIn("1 completed", stdout)
        # Per-change table
        self.assertIn("=== Per-Change Metrics ===", stdout)
        self.assertIn(cid, stdout)
        self.assertIn("completed", stdout)
        # Plan summary shows total cost
        self.assertIn("$0.39", stdout)
        # Stage aggregates
        self.assertIn("=== Stage Aggregates ===", stdout)
        # Model leaderboard
        self.assertIn("=== Model Leaderboard ===", stdout)
        self.assertIn("openai:gpt-4o", stdout)

    def test_report_surfaces_pending_manual_tasks_checklist(self) -> None:
        plan_name = "manual-plan"
        cid = "ch-manual"
        plan_path = self._write_plan_toml(plan_name, [cid])

        records = [
            self._build_record(
                stage="implement", change_id=cid, plan_name=plan_name,
                duration_ms=120000, input_tokens=50000, output_tokens=10000,
                estimated_cost=0.25, cost_status="estimated",
            ),
            self._build_record(
                stage="review", change_id=cid, plan_name=plan_name,
                duration_ms=30000, input_tokens=20000, output_tokens=5000,
                estimated_cost=0.10, cost_status="estimated",
                verdict="pass",
            ),
            self._build_record(
                stage="archive", change_id=cid, plan_name=plan_name,
                duration_ms=10000, input_tokens=8000, output_tokens=2000,
                estimated_cost=0.04, cost_status="estimated",
            ),
        ]
        self._write_telemetry(plan_name, records)
        self._write_state(plan_name, {
            "plan": plan_name, "approvals": [],
            "changes": {
                cid: {
                    "status": "done", "round": 1, "phase": "done",
                    "manual_tasks_pending": [
                        "4.2 Plant fixtures and run the live jobs (manual)",
                    ],
                },
            },
        })

        rc, stdout, _ = self._run_report(
            plan_path=plan_path, plan_name=plan_name, json=True,
        )
        self.assertEqual(rc, 0)
        data = json.loads(stdout)
        cm = [c for c in data["change_metrics"] if c["change_id"] == cid][0]
        self.assertEqual(
            cm["manual_tasks_pending"],
            ["4.2 Plant fixtures and run the live jobs (manual)"],
        )

        rc, stdout, _ = self._run_report(
            plan_path=plan_path, plan_name=plan_name,
        )
        self.assertEqual(rc, 0)
        self.assertIn("=== Manual Follow-Up (operator checklist) ===", stdout)
        self.assertIn("4.2 Plant fixtures and run the live jobs (manual)", stdout)

    # -- 7.2 table output for multi-change plan (mixed cost statuses)

    def test_table_output_multi_change_mixed_costs(self) -> None:
        plan_name = "multi-plan"
        cids = ["ch-alpha", "ch-beta"]
        plan_path = self._write_plan_toml(plan_name, cids)

        records: list[dict] = []
        for cid in cids:
            cost_status = "estimated" if cid == "ch-alpha" else "unresolved"
            records.append(self._build_record(
                stage="implement", change_id=cid, plan_name=plan_name,
                duration_ms=60000, input_tokens=30000, output_tokens=6000,
                estimated_cost=0.15 if cost_status == "estimated" else None,
                cost_status=cost_status,
            ))
            records.append(self._build_record(
                stage="review", change_id=cid, plan_name=plan_name,
                duration_ms=20000, input_tokens=15000, output_tokens=3000,
                estimated_cost=0.08 if cost_status == "estimated" else None,
                cost_status=cost_status, verdict="pass",
            ))
            records.append(self._build_record(
                stage="archive", change_id=cid, plan_name=plan_name,
                duration_ms=8000, input_tokens=5000, output_tokens=1000,
                estimated_cost=0.02 if cost_status == "estimated" else None,
                cost_status=cost_status,
            ))
        self._write_telemetry(plan_name, records)
        self._write_state(plan_name, {
            "plan": plan_name, "approvals": [],
            "changes": {
                cid: {"status": "done", "round": 1, "phase": "done"}
                for cid in cids
            },
        })

        rc, stdout, stderr = self._run_report(
            plan_path=plan_path, plan_name=plan_name,
        )
        self.assertEqual(rc, 0)
        self.assertIn("2 completed", stdout)
        self.assertIn("ch-alpha", stdout)
        self.assertIn("ch-beta", stdout)
        # One change has estimated costs
        self.assertIn("estimated", stdout)
        # The other has unresolved cost
        self.assertIn("unresolved", stdout)

    # -- 7.3 table output for failed plan with max-rounds and review failures

    def test_table_output_failed_plan(self) -> None:
        plan_name = "fail-plan"
        cid = "ch-fail"
        plan_path = self._write_plan_toml(plan_name, [cid])

        records = [
            self._build_record(
                stage="implement", change_id=cid, plan_name=plan_name,
                duration_ms=90000, cost_status="unresolved",
                round_num=1,
            ),
            self._build_record(
                stage="review", change_id=cid, plan_name=plan_name,
                duration_ms=25000, cost_status="unresolved",
                verdict="fail", critical=1, round_num=1,
            ),
            self._build_record(
                stage="implement", change_id=cid, plan_name=plan_name,
                duration_ms=90000, cost_status="unresolved",
                round_num=2,
            ),
            self._build_record(
                stage="review", change_id=cid, plan_name=plan_name,
                duration_ms=25000, cost_status="unresolved",
                verdict="fail", critical=1, round_num=2,
            ),
        ]
        self._write_telemetry(plan_name, records)
        self._write_state(plan_name, {
            "plan": plan_name, "approvals": [],
            "changes": {
                cid: {
                    "status": "failed", "round": 2, "phase": "implement",
                    "max_rounds": 2, "no_progress_streak": 1,
                },
            },
        })

        rc, stdout, stderr = self._run_report(
            plan_path=plan_path, plan_name=plan_name,
        )
        self.assertEqual(rc, 0)
        self.assertIn("1 failed", stdout)
        self.assertIn("failed", stdout)
        self.assertIn("2", stdout)  # total rounds
        self.assertIn("2", stdout)  # review failures
        # no_progress should show "yes" in table output (formatted by _fmt_bool)
        self.assertIn("yes", stdout)
        # max_rounds_exceeded
        self.assertIn("yes", stdout)

    # -- 7.4 JSON output structure and stability

    def test_json_byte_identical_across_two_runs(self) -> None:
        plan_name = "stable-plan"
        cid = "ch-stable"
        plan_path = self._write_plan_toml(plan_name, [cid])

        records = [
            self._build_record(
                stage="implement", change_id=cid, plan_name=plan_name,
                duration_ms=60000, estimated_cost=0.20,
            ),
            self._build_record(
                stage="review", change_id=cid, plan_name=plan_name,
                duration_ms=20000, estimated_cost=0.08,
                verdict="pass",
            ),
            self._build_record(
                stage="archive", change_id=cid, plan_name=plan_name,
                duration_ms=8000, estimated_cost=0.03,
            ),
        ]
        self._write_telemetry(plan_name, records)
        self._write_state(plan_name, {
            "plan": plan_name, "approvals": [],
            "changes": {
                cid: {"status": "done", "round": 1, "phase": "done"},
            },
        })

        rc1, stdout1, _ = self._run_report(
            plan_path=plan_path, plan_name=plan_name, json=True,
        )
        rc2, stdout2, _ = self._run_report(
            plan_path=plan_path, plan_name=plan_name, json=True,
        )
        self.assertEqual(rc1, 0)
        self.assertEqual(rc2, 0)
        self.assertEqual(stdout1, stdout2)

    # -- 7.5 JSON output matches aggregation dataclass fields

    def test_json_structure_matches_dataclasses(self) -> None:
        plan_name = "struct-plan"
        cid = "ch-struct"
        plan_path = self._write_plan_toml(plan_name, [cid])

        records = [
            self._build_record(
                stage="implement", change_id=cid, plan_name=plan_name,
                estimated_cost=0.30,
            ),
            self._build_record(
                stage="review", change_id=cid, plan_name=plan_name,
                estimated_cost=0.10, verdict="pass",
            ),
            self._build_record(
                stage="archive", change_id=cid, plan_name=plan_name,
                estimated_cost=0.05,
            ),
        ]
        self._write_telemetry(plan_name, records)
        self._write_state(plan_name, {
            "plan": plan_name, "approvals": [],
            "changes": {
                cid: {"status": "done", "round": 1, "phase": "done"},
            },
        })

        rc, stdout, _ = self._run_report(
            plan_path=plan_path, plan_name=plan_name, json=True,
        )
        self.assertEqual(rc, 0)
        data = json.loads(stdout)
        # Top-level keys
        for key in ("command", "plan_name", "run_id", "filters",
                     "plan_metrics", "change_metrics",
                     "stage_aggregates", "model_leaderboard", "warnings"):
            self.assertIn(key, data, f"Missing top-level key: {key}")

        # PlanMetrics keys
        pm = data["plan_metrics"]
        for key in ("plan_name", "run_id", "total_changes", "completed_changes",
                     "failed_changes", "blocked_changes", "incomplete_changes",
                     "completion_rate", "success_rate", "total_duration_ms",
                     "total_tokens", "total_estimated_cost",
                     "estimated_cost_changes", "unresolved_cost_changes",
                     "unknown_cost_changes"):
            self.assertIn(key, pm, f"Missing plan_metrics key: {key}")

        # ChangeMetrics keys
        self.assertGreaterEqual(len(data["change_metrics"]), 1)
        cm = data["change_metrics"][0]
        for key in ("change_id", "status", "total_rounds", "duration_ms",
                     "tokens", "estimated_cost", "cost_status",
                     "first_pass_review", "review_failures", "no_progress",
                     "max_rounds_exceeded", "archive_failed", "fast_check_failed"):
            self.assertIn(key, cm, f"Missing change_metrics key: {key}")

        # StageAggregates keys
        sa = data["stage_aggregates"]
        for key in ("average_rounds", "median_rounds",
                     "average_duration_implement", "average_duration_review",
                     "average_duration_archive", "review_failure_rate",
                     "average_tokens_per_change", "average_cost_per_change"):
            self.assertIn(key, sa, f"Missing stage_aggregates key: {key}")

        # ModelLeaderboardEntry keys on at least one entry
        self.assertGreater(len(data["model_leaderboard"]), 0,
                           "Leaderboard must not be empty for a completed change")
        ml = data["model_leaderboard"][0]
        for key in ("implementer_model", "reviewer_model", "archiver_model",
                     "change_count", "success_rate", "first_pass_rate",
                     "average_rounds", "average_duration_ms",
                     "average_tokens", "average_cost"):
            self.assertIn(key, ml, f"Missing model_leaderboard key: {key}")
        # No entry may be a partial-role row — every entry must have all
        # three role models populated.
        for entry in data["model_leaderboard"]:
            self.assertIsNotNone(entry.get("implementer_model"),
                                 f"Partial-role leaderboard entry (missing implementer_model): {entry}")
            self.assertIsNotNone(entry.get("reviewer_model"),
                                 f"Partial-role leaderboard entry (missing reviewer_model): {entry}")
            self.assertIsNotNone(entry.get("archiver_model"),
                                 f"Partial-role leaderboard entry (missing archiver_model): {entry}")

    # -- 7.6 --change filter

    def test_change_filter(self) -> None:
        plan_name = "filter-change-plan"
        cids = ["ch-one", "ch-two"]
        plan_path = self._write_plan_toml(plan_name, cids)

        records: list[dict] = []
        for cid in cids:
            records.append(self._build_record(
                stage="implement", change_id=cid, plan_name=plan_name,
                duration_ms=60000, estimated_cost=0.15,
            ))
            records.append(self._build_record(
                stage="review", change_id=cid, plan_name=plan_name,
                duration_ms=20000, estimated_cost=0.08,
                verdict="pass",
            ))
            records.append(self._build_record(
                stage="archive", change_id=cid, plan_name=plan_name,
                duration_ms=8000, estimated_cost=0.03,
            ))
        self._write_telemetry(plan_name, records)
        self._write_state(plan_name, {
            "plan": plan_name, "approvals": [],
            "changes": {
                cid: {"status": "done", "round": 1, "phase": "done"}
                for cid in cids
            },
        })

        # Without filter, both changes appear
        rc_full, stdout_full, _ = self._run_report(
            plan_path=plan_path, plan_name=plan_name,
        )
        self.assertEqual(rc_full, 0)
        self.assertIn("ch-one", stdout_full)
        self.assertIn("ch-two", stdout_full)

        # With --change filter, only ch-one appears in per-change table
        rc, stdout, _ = self._run_report(
            plan_path=plan_path, plan_name=plan_name,
            change="ch-one",
        )
        self.assertEqual(rc, 0)
        self.assertIn("ch-one", stdout)
        self.assertNotIn("ch-two", stdout)

        # JSON with --change filter
        rc_json, stdout_json, _ = self._run_report(
            plan_path=plan_path, plan_name=plan_name,
            change="ch-one", json=True,
        )
        self.assertEqual(rc_json, 0)
        data = json.loads(stdout_json)
        change_ids = [c["change_id"] for c in data["change_metrics"]]
        self.assertEqual(change_ids, ["ch-one"])
        self.assertEqual(data["filters"]["change"], "ch-one")

    # -- 7.7 --run-id filter

    def test_run_id_filter(self) -> None:
        plan_name = "multi-run-plan"
        cid = "ch-run"
        plan_path = self._write_plan_toml(plan_name, [cid])

        run_a_records = [
            self._build_record(
                stage="implement", change_id=cid, plan_name=plan_name,
                run_id="run-A", estimated_cost=0.10,
            ),
            self._build_record(
                stage="review", change_id=cid, plan_name=plan_name,
                run_id="run-A", estimated_cost=0.05, verdict="pass",
            ),
            self._build_record(
                stage="archive", change_id=cid, plan_name=plan_name,
                run_id="run-A", estimated_cost=0.02,
            ),
        ]
        run_b_records = [
            self._build_record(
                stage="implement", change_id=cid, plan_name=plan_name,
                run_id="run-B", estimated_cost=0.30,
                started_at="2026-07-02T10:00:00Z",  # later start
            ),
            self._build_record(
                stage="review", change_id=cid, plan_name=plan_name,
                run_id="run-B", estimated_cost=0.12, verdict="pass",
                started_at="2026-07-02T10:01:00Z",
            ),
            self._build_record(
                stage="archive", change_id=cid, plan_name=plan_name,
                run_id="run-B", estimated_cost=0.06,
                started_at="2026-07-02T10:02:00Z",
            ),
        ]
        all_records = run_a_records + run_b_records
        self._write_telemetry(plan_name, all_records)
        self._write_state(plan_name, {
            "plan": plan_name, "approvals": [],
            "changes": {
                cid: {"status": "done", "round": 1, "phase": "done"},
            },
        })

        # Default (latest): should pick run-B
        rc, stdout, _ = self._run_report(
            plan_path=plan_path, plan_name=plan_name,
        )
        self.assertEqual(rc, 0)
        self.assertIn("run-B", stdout)

        # Explicit filter to run-A
        rc_a, stdout_a, _ = self._run_report(
            plan_path=plan_path, plan_name=plan_name,
            run_id="run-A",
        )
        self.assertEqual(rc_a, 0)
        self.assertIn("run-A", stdout_a)

        # JSON with run-id filter
        rc_json, stdout_json, _ = self._run_report(
            plan_path=plan_path, plan_name=plan_name,
            run_id="run-A", json=True,
        )
        self.assertEqual(rc_json, 0)
        data = json.loads(stdout_json)
        self.assertEqual(data["run_id"], "run-A")
        self.assertEqual(data["filters"]["run_id"], "run-A")

    # -- 7.8 --model filter

    def test_model_filter(self) -> None:
        plan_name = "model-filter-plan"
        cids = ["ch-gpt", "ch-claude"]
        plan_path = self._write_plan_toml(plan_name, cids)

        records: list[dict] = []
        # ch-gpt uses openai:gpt-4o
        records.append(self._build_record(
            stage="implement", change_id="ch-gpt", plan_name=plan_name,
            provider="openai", model_id="gpt-4o", estimated_cost=0.15,
        ))
        records.append(self._build_record(
            stage="review", change_id="ch-gpt", plan_name=plan_name,
            provider="openai", model_id="gpt-4o", estimated_cost=0.08,
            verdict="pass",
        ))
        records.append(self._build_record(
            stage="archive", change_id="ch-gpt", plan_name=plan_name,
            provider="openai", model_id="gpt-4o", estimated_cost=0.03,
        ))
        # ch-claude uses anthropic:claude-sonnet
        records.append(self._build_record(
            stage="implement", change_id="ch-claude", plan_name=plan_name,
            provider="anthropic", model_id="claude-sonnet", estimated_cost=0.20,
        ))
        records.append(self._build_record(
            stage="review", change_id="ch-claude", plan_name=plan_name,
            provider="anthropic", model_id="claude-sonnet", estimated_cost=0.10,
            verdict="pass",
        ))
        records.append(self._build_record(
            stage="archive", change_id="ch-claude", plan_name=plan_name,
            provider="anthropic", model_id="claude-sonnet", estimated_cost=0.04,
        ))
        self._write_telemetry(plan_name, records)
        self._write_state(plan_name, {
            "plan": plan_name, "approvals": [],
            "changes": {
                cid: {"status": "done", "round": 1, "phase": "done"}
                for cid in cids
            },
        })

        # Without model filter, both models appear
        rc_full, stdout_full, _ = self._run_report(
            plan_path=plan_path, plan_name=plan_name,
        )
        self.assertEqual(rc_full, 0)
        self.assertIn("gpt-4o", stdout_full)
        self.assertIn("claude-sonnet", stdout_full)

        # Filter by model substring (case-insensitive)
        rc, stdout, _ = self._run_report(
            plan_path=plan_path, plan_name=plan_name,
            model="claude",
        )
        self.assertEqual(rc, 0)
        self.assertIn("claude-sonnet", stdout)
        self.assertNotIn("gpt-4o", stdout)

    # -- 7.9 --stage filter

    def test_stage_filter(self) -> None:
        plan_name = "stage-filter-plan"
        cid = "ch-stage"
        plan_path = self._write_plan_toml(plan_name, [cid])

        records = [
            self._build_record(
                stage="implement", change_id=cid, plan_name=plan_name,
                estimated_cost=0.15, provider="openai", model_id="gpt-4o",
            ),
            self._build_record(
                stage="review", change_id=cid, plan_name=plan_name,
                estimated_cost=0.08, verdict="pass",
                provider="openai", model_id="gpt-4o-mini",
            ),
            self._build_record(
                stage="archive", change_id=cid, plan_name=plan_name,
                estimated_cost=0.03,
                provider="openai", model_id="gpt-4o",
            ),
        ]
        self._write_telemetry(plan_name, records)
        self._write_state(plan_name, {
            "plan": plan_name, "approvals": [],
            "changes": {
                cid: {"status": "done", "round": 1, "phase": "done"},
            },
        })

        # Stage filter: implement — leaderboard should only have implement entries
        rc, stdout, _ = self._run_report(
            plan_path=plan_path, plan_name=plan_name,
            stage="implement",
        )
        self.assertEqual(rc, 0)
        self.assertIn("Avg Implement Duration", stdout)
        self.assertNotIn("Avg Review Duration", stdout)
        self.assertNotIn("Avg Archive Duration", stdout)

        # Stage filter: review
        rc_r, stdout_r, _ = self._run_report(
            plan_path=plan_path, plan_name=plan_name,
            stage="review",
        )
        self.assertEqual(rc_r, 0)
        self.assertIn("Avg Review Duration", stdout_r)
        self.assertNotIn("Avg Implement Duration", stdout_r)

        # Invalid stage rejected
        plan_path2 = self._write_plan_toml("bad-stage-plan", [cid])
        records2 = [
            self._build_record(
                stage="implement", change_id=cid,
                plan_name="bad-stage-plan", estimated_cost=0.10,
            ),
            self._build_record(
                stage="review", change_id=cid,
                plan_name="bad-stage-plan", estimated_cost=0.05,
                verdict="pass",
            ),
        ]
        self._write_telemetry("bad-stage-plan", records2)
        self._write_state("bad-stage-plan", {
            "plan": "bad-stage-plan", "approvals": [],
            "changes": {
                cid: {"status": "done", "round": 1, "phase": "done"},
            },
        })
        rc_bad, _, stderr_bad = self._run_report(
            plan_path=plan_path2, stage="invalid",
        )
        self.assertEqual(rc_bad, 2)
        self.assertIn("invalid stage", stderr_bad.lower())

    # -- 7.10 empty telemetry: graceful output with warnings

    def test_empty_telemetry(self) -> None:
        plan_name = "empty-plan"
        cid = "ch-empty"
        plan_path = self._write_plan_toml(plan_name, [cid])

        # No telemetry written; just state with a change
        self._write_state(plan_name, {
            "plan": plan_name, "approvals": [],
            "changes": {
                cid: {"status": "running", "round": 0, "phase": "implement"},
            },
        })

        rc, stdout, _ = self._run_report(
            plan_path=plan_path, plan_name=plan_name,
        )
        self.assertEqual(rc, 0)
        self.assertIn("=== Plan Summary ===", stdout)
        self.assertIn(plan_name, stdout)
        # State has ch-empty registered, so "1 total" with "0 completed"
        self.assertIn("1 total", stdout)
        self.assertIn("0 completed", stdout)
        # Warnings section should mention missing telemetry
        self.assertIn("=== Warnings", stdout)
        self.assertIn("telemetry file not found", stdout)

    # -- 7.11 unresolved and unavailable cost rendering

    def test_cost_rendering(self) -> None:
        # Direct unit tests on _fmt_cost for all cost statuses
        # estimated with value
        self.assertEqual(report._fmt_cost(0.50, "estimated"), "$0.50")
        # estimated with None value (shouldn't happen in practice but safe)
        self.assertEqual(report._fmt_cost(None, "estimated"), "—")
        # unresolved
        self.assertEqual(report._fmt_cost(0.50, "unresolved"), "unresolved")
        # unavailable
        self.assertEqual(report._fmt_cost(None, "unavailable"), "unavailable")
        # partial
        self.assertEqual(report._fmt_cost(0.30, "partial"), "$0.30")
        # unknown status falls through as —
        self.assertEqual(report._fmt_cost(None, "unknown"), "—")

        # Also test via table output: create unresolved change
        plan_name = "cost-render-plan"
        cid = "ch-cost"
        plan_path = self._write_plan_toml(plan_name, [cid])
        records = [
            self._build_record(
                stage="implement", change_id=cid, plan_name=plan_name,
                cost_status="unresolved", estimated_cost=None,
                duration_ms=60000,
            ),
        ]
        self._write_telemetry(plan_name, records)
        self._write_state(plan_name, {
            "plan": plan_name, "approvals": [],
            "changes": {
                cid: {"status": "running", "round": 1, "phase": "implement"},
            },
        })
        rc, stdout, _ = self._run_report(plan_path=plan_path)
        self.assertEqual(rc, 0)
        # Table should show "unresolved" in cost columns
        self.assertIn("unresolved", stdout)

    # -- 7.12 zero estimated cost rendered as "$0.00"

    def test_zero_cost_rendered_as_dollar_zero(self) -> None:
        self.assertEqual(report._fmt_cost(0.0, "estimated"), "$0.00")
        self.assertEqual(report._fmt_cost(0.0, "partial"), "$0.00")

        # Verify zero-cost appears in table as "$0.00" not "unresolved"
        plan_name = "zero-cost-plan"
        cid = "ch-zero"
        plan_path = self._write_plan_toml(plan_name, [cid])
        records = [
            self._build_record(
                stage="implement", change_id=cid, plan_name=plan_name,
                estimated_cost=0.0, cost_status="estimated",
                duration_ms=1000,
            ),
        ]
        self._write_telemetry(plan_name, records)
        self._write_state(plan_name, {
            "plan": plan_name, "approvals": [],
            "changes": {
                cid: {"status": "running", "round": 1, "phase": "implement"},
            },
        })
        rc, stdout, _ = self._run_report(plan_path=plan_path)
        self.assertEqual(rc, 0)
        self.assertIn("$0.00", stdout)
        # The per-change cost status should be "estimated", not "unresolved"
        # (plan summary contains "0 unresolved" in the cost breakdown line,
        # but the per-change status column should show "estimated")
        lines = [l for l in stdout.splitlines() if "ch-zero" in l]
        self.assertTrue(any("estimated" in l for l in lines))

    # -- 7.13 long change/model ID truncation in table mode only

    def test_long_id_truncation_in_table(self) -> None:
        # Unit test _truncate_id
        self.assertEqual(report._truncate_id(None), "—")
        short = "short-id"
        self.assertEqual(report._truncate_id(short), short)
        exact30 = "a" * 30
        self.assertEqual(report._truncate_id(exact30), exact30)
        long31 = "a" * 31
        truncated = report._truncate_id(long31)
        self.assertLess(len(truncated), len(long31))
        self.assertTrue(truncated.endswith("…"))
        self.assertEqual(len(truncated), 30)

        # Long ID appears truncated in table output but not in JSON
        plan_name = "trunc-plan"
        long_cid = "very-long-change-id-that-exceeds-thirty-chars"
        plan_path = self._write_plan_toml(plan_name, [long_cid])
        records = [
            self._build_record(
                stage="implement", change_id=long_cid, plan_name=plan_name,
                estimated_cost=0.10, duration_ms=60000,
            ),
            self._build_record(
                stage="review", change_id=long_cid, plan_name=plan_name,
                estimated_cost=0.05, verdict="pass",
            ),
        ]
        self._write_telemetry(plan_name, records)
        self._write_state(plan_name, {
            "plan": plan_name, "approvals": [],
            "changes": {
                long_cid: {"status": "done", "round": 1, "phase": "done"},
            },
        })

        # Table mode: truncated
        rc_table, stdout_table, _ = self._run_report(plan_path=plan_path)
        self.assertEqual(rc_table, 0)
        self.assertNotIn(long_cid, stdout_table)  # full ID truncated
        self.assertIn("…", stdout_table)          # truncation marker

        # JSON mode: full ID preserved
        rc_json, stdout_json, _ = self._run_report(
            plan_path=plan_path, json=True,
        )
        self.assertEqual(rc_json, 0)
        data = json.loads(stdout_json)
        self.assertIn(long_cid, stdout_json)
        self.assertEqual(data["change_metrics"][0]["change_id"], long_cid)

    # -- 6.5 unknown model identity shows "unknown" in leaderboard output

    def test_unknown_model_identity_shows_unknown_in_leaderboard(self) -> None:
        """Unknown model identity appears as 'unknown' in leaderboard
        instead of dropping rows entirely."""
        plan_name = "unknown-model-plan"
        cid_known = "ch-known"
        cid_unknown = "ch-unknown"
        plan_path = self._write_plan_toml(plan_name, [cid_known, cid_unknown])

        records = [
            # Change with known model identity
            self._build_record(
                stage="implement", change_id=cid_known, plan_name=plan_name,
                estimated_cost=0.10, provider="openai", model_id="gpt-4o",
            ),
            self._build_record(
                stage="review", change_id=cid_known, plan_name=plan_name,
                estimated_cost=0.05, verdict="pass",
                provider="openai", model_id="gpt-4o",
            ),
            self._build_record(
                stage="archive", change_id=cid_known, plan_name=plan_name,
                estimated_cost=0.04, provider="openai", model_id="gpt-4o",
            ),
            # Change with unknown model identity (empty provider/model_id)
            self._build_record(
                stage="implement", change_id=cid_unknown, plan_name=plan_name,
                estimated_cost=0.10, provider="", model_id="",
            ),
            self._build_record(
                stage="review", change_id=cid_unknown, plan_name=plan_name,
                estimated_cost=0.05, verdict="pass",
                provider="", model_id="",
            ),
            self._build_record(
                stage="archive", change_id=cid_unknown, plan_name=plan_name,
                estimated_cost=0.04, provider="", model_id="",
            ),
        ]
        self._write_telemetry(plan_name, records)
        self._write_state(plan_name, {
            "plan": plan_name, "approvals": [],
            "changes": {
                cid_known: {"status": "done", "round": 1, "phase": "done"},
                cid_unknown: {"status": "done", "round": 1, "phase": "done"},
            },
        })

        # Table mode: "unknown" appears in leaderboard output
        rc, stdout, _ = self._run_report(plan_path=plan_path)
        self.assertEqual(rc, 0)
        self.assertIn("unknown", stdout)

        # JSON mode: model_leaderboard contains entries with "unknown" model
        rc_json, stdout_json, _ = self._run_report(
            plan_path=plan_path, json=True,
        )
        self.assertEqual(rc_json, 0)
        data = json.loads(stdout_json)

        unknown_entries = [
            e for e in data["model_leaderboard"]
            if e.get("implementer_model") == "unknown"
            or e.get("reviewer_model") == "unknown"
            or e.get("archiver_model") == "unknown"
        ]
        self.assertGreater(
            len(unknown_entries), 0,
            "Leaderboard should contain entries with 'unknown' model",
        )

    # -- 7.14 report does not modify telemetry or state files

    def test_report_does_not_modify_telemetry_or_state(self) -> None:
        plan_name = "idempotent-plan"
        cid = "ch-idem"
        plan_path = self._write_plan_toml(plan_name, [cid])

        records = [
            self._build_record(
                stage="implement", change_id=cid, plan_name=plan_name,
                estimated_cost=0.20,
            ),
            self._build_record(
                stage="review", change_id=cid, plan_name=plan_name,
                estimated_cost=0.08, verdict="pass",
            ),
            self._build_record(
                stage="archive", change_id=cid, plan_name=plan_name,
                estimated_cost=0.04,
            ),
        ]
        self._write_telemetry(plan_name, records)
        state = {
            "plan": plan_name, "approvals": [],
            "changes": {
                cid: {"status": "done", "round": 1, "phase": "done"},
            },
        }
        self._write_state(plan_name, state)

        # Snapshot file contents before report
        telemetry_path = self.repo / ".opsx-plan" / "telemetry" / f"{plan_name}.jsonl"
        state_path = self.repo / ".opsx-plan" / f"{plan_name}.state.json"
        plan_toml_path = plan_path

        tele_before = telemetry_path.read_text(encoding="utf-8")
        state_before = state_path.read_text(encoding="utf-8")
        plan_before = plan_toml_path.read_text(encoding="utf-8")

        # Run table mode report
        rc, stdout, _ = self._run_report(plan_path=plan_path)
        self.assertEqual(rc, 0)

        # Run JSON mode report
        rc2, stdout2, _ = self._run_report(plan_path=plan_path, json=True)
        self.assertEqual(rc2, 0)

        # Verify files are unchanged
        self.assertEqual(telemetry_path.read_text(encoding="utf-8"), tele_before,
                         "telemetry file was modified by report command")
        self.assertEqual(state_path.read_text(encoding="utf-8"), state_before,
                         "state file was modified by report command")
        self.assertEqual(plan_toml_path.read_text(encoding="utf-8"), plan_before,
                         "plan TOML was modified by report command")

        # Verify report command does not create new files in .opsx-plan
        opsx_plan_dir = self.repo / ".opsx-plan"
        opsx_contents = set()
        for root, dirs, files in os.walk(str(opsx_plan_dir)):
            for f in files:
                opsx_contents.add(Path(root) / f)
        expected = {telemetry_path, state_path}
        self.assertEqual(opsx_contents, expected,
                         "report command created unexpected files")

    # -- formatting helpers ---------------------------------------------------

    def test_fmt_duration(self) -> None:
        self.assertEqual(report._fmt_duration(None), "—")
        self.assertEqual(report._fmt_duration(0), "0m0s")
        self.assertEqual(report._fmt_duration(90000), "1m30s")
        self.assertEqual(report._fmt_duration(60000), "1m0s")
        self.assertEqual(report._fmt_duration(61000), "1m1s")
        self.assertEqual(report._fmt_duration(3599999), "59m59s")

    def test_fmt_tokens(self) -> None:
        self.assertEqual(report._fmt_tokens(None), "—")
        self.assertEqual(report._fmt_tokens(0), "0")
        self.assertEqual(report._fmt_tokens(500), "500")
        self.assertEqual(report._fmt_tokens(1500), "1.5K")
        self.assertEqual(report._fmt_tokens(2_500_000), "2.5M")

    def test_fmt_pct(self) -> None:
        self.assertEqual(report._fmt_pct(None), "—")
        self.assertEqual(report._fmt_pct(0.0), "0.0%")
        self.assertEqual(report._fmt_pct(0.5), "50.0%")
        self.assertEqual(report._fmt_pct(1.0), "100.0%")
        self.assertEqual(report._fmt_pct(0.333), "33.3%")

    def test_fmt_bool(self) -> None:
        self.assertEqual(report._fmt_bool(None), "—")
        self.assertEqual(report._fmt_bool(True), "yes")
        self.assertEqual(report._fmt_bool(False), "no")

    def test_invalid_stage_rejected(self) -> None:
        plan_name = "invalid-stage-plan"
        cid = "ch-invalid-stage"
        plan_path = self._write_plan_toml(plan_name, [cid])
        records = [
            self._build_record(
                stage="implement", change_id=cid, plan_name=plan_name,
                estimated_cost=0.10,
            ),
        ]
        self._write_telemetry(plan_name, records)
        self._write_state(plan_name, {
            "plan": plan_name, "approvals": [],
            "changes": {
                cid: {"status": "running", "round": 1, "phase": "implement"},
            },
        })
        rc, _, stderr = self._run_report(
            plan_path=plan_path, stage="bogus",
        )
        self.assertEqual(rc, 2)
        self.assertIn("invalid stage", stderr)


    # -- regression: triple leaderboard includes failed model combinations -----

    def test_triple_leaderboard_includes_failed_combinations(self) -> None:
        """The full model-combination (triple) leaderboard must include
        rows for every model triplet that touched a change — including
        failed, blocked, and incomplete changes — not only completed ones."""
        plan_name = "triple-fail-plan"
        cid = "ch-triple-fail"
        plan_path = self._write_plan_toml(plan_name, [cid])

        records = [
            self._build_record(
                stage="implement", change_id=cid, plan_name=plan_name,
                provider="openai", model_id="gpt-4o",
                cost_status="unresolved", estimated_cost=None,
            ),
            self._build_record(
                stage="review", change_id=cid, plan_name=plan_name,
                provider="openai", model_id="gpt-4o-mini",
                cost_status="unresolved", estimated_cost=None,
                verdict="fail", critical=1,
            ),
            self._build_record(
                stage="archive", change_id=cid, plan_name=plan_name,
                provider="openai", model_id="gpt-4o",
                cost_status="unresolved", estimated_cost=None,
            ),
        ]
        self._write_telemetry(plan_name, records)
        self._write_state(plan_name, {
            "plan": plan_name, "approvals": [],
            "changes": {
                cid: {
                    "status": "failed", "round": 1, "phase": "implement",
                    "max_rounds": 1, "no_progress_streak": 0,
                },
            },
        })

        # Table mode: the triple leaderboard should contain the model triplet
        rc, stdout, _ = self._run_report(plan_path=plan_path)
        self.assertEqual(rc, 0)
        self.assertIn("=== Model Leaderboard ===", stdout)
        # The triple row with openai:gpt-4o / openai:gpt-4o-mini / openai:gpt-4o
        # should be present even though the change failed.
        self.assertIn("gpt-4o", stdout)

        # JSON mode: the leaderboard must contain an entry for this
        # (failed) triple combination with change_count >= 1.
        rc_json, stdout_json, _ = self._run_report(
            plan_path=plan_path, json=True,
        )
        self.assertEqual(rc_json, 0)
        data = json.loads(stdout_json)
        self.assertIsNotNone(data.get("model_leaderboard"))

        triple_entries = [
            e for e in data["model_leaderboard"]
            if (e.get("implementer_model") and e.get("reviewer_model")
                and e.get("archiver_model"))
        ]
        self.assertGreater(
            len(triple_entries), 0,
            "Triple leaderboard must include the failed model combination",
        )
        self.assertGreaterEqual(
            triple_entries[0].get("change_count", 0), 1,
            "Failed combination must have change_count >= 1",
        )

    # -- regression: leaderboard has no partial-role rows -------------------

    def test_leaderboard_has_no_partial_role_rows(self) -> None:
        """Every leaderboard entry must have all three role model fields
        populated (implementer, reviewer, archiver).  Per-role-only rows
        (e.g. implementer-only) must never appear."""
        plan_name = "no-partial-plan"
        cids = ["ch-alpha", "ch-beta"]
        plan_path = self._write_plan_toml(plan_name, cids)

        records: list[dict] = []
        for cid in cids:
            impl_p, impl_m = ("openai", "gpt-4o") if cid == "ch-alpha" else ("anthropic", "claude-sonnet")
            rev_p, rev_m = ("openai", "gpt-4o-mini") if cid == "ch-alpha" else ("", "")
            arch_p, arch_m = ("openai", "gpt-4o") if cid == "ch-alpha" else ("", "")

            records.append(self._build_record(
                stage="implement", change_id=cid, plan_name=plan_name,
                provider=impl_p, model_id=impl_m, estimated_cost=0.10,
            ))
            records.append(self._build_record(
                stage="review", change_id=cid, plan_name=plan_name,
                provider=rev_p, model_id=rev_m, estimated_cost=0.05,
                verdict="pass",
            ))
            records.append(self._build_record(
                stage="archive", change_id=cid, plan_name=plan_name,
                provider=arch_p, model_id=arch_m, estimated_cost=0.03,
            ))
        self._write_telemetry(plan_name, records)
        self._write_state(plan_name, {
            "plan": plan_name, "approvals": [],
            "changes": {
                cid: {"status": "done", "round": 1, "phase": "done"}
                for cid in cids
            },
        })

        rc, stdout_json, _ = self._run_report(
            plan_path=plan_path, plan_name=plan_name, json=True,
        )
        self.assertEqual(rc, 0)
        data = json.loads(stdout_json)
        self.assertIsNotNone(data.get("model_leaderboard"))
        self.assertGreater(len(data["model_leaderboard"]), 0,
                           "Leaderboard must not be empty")

        # Verify complete triple entry exists (ch-alpha has all three roles with known models)
        complete_entries = [
            e for e in data["model_leaderboard"]
            if (e.get("implementer_model") and e.get("implementer_model") != "unknown" and
                e.get("reviewer_model") and e.get("reviewer_model") != "unknown" and
                e.get("archiver_model") and e.get("archiver_model") != "unknown")
        ]
        self.assertEqual(len(complete_entries), 1,
                         "Should have exactly one complete triple entry (ch-alpha)")
        complete = complete_entries[0]
        self.assertEqual(complete["implementer_model"], "openai:gpt-4o")
        self.assertEqual(complete["reviewer_model"], "openai:gpt-4o-mini")
        self.assertEqual(complete["archiver_model"], "openai:gpt-4o")

        # Verify partial entry exists for ch-beta (only implementer known, others marked "unknown")
        partial_entries = [
            e for e in data["model_leaderboard"]
            if e.get("implementer_model") == "anthropic:claude-sonnet"
        ]
        self.assertEqual(len(partial_entries), 1,
                         "Should have exactly one entry for ch-beta (partial with unknown roles)")
        partial = partial_entries[0]
        self.assertEqual(partial["implementer_model"], "anthropic:claude-sonnet")
        self.assertEqual(partial.get("reviewer_model"), "unknown",
                         "Partial entry should have 'unknown' for unknown reviewer_model")
        self.assertEqual(partial.get("archiver_model"), "unknown",
                         "Partial entry should have 'unknown' for unknown archiver_model")

    # -- regression: avg tokens/change displayed even when cost is unresolved

    def test_avg_tokens_present_when_cost_unresolved(self) -> None:
        """Stage aggregates must still display avg tokens per change even
        when every change has unresolved cost — tokens are available
        independently of cost estimation."""
        plan_name = "tokens-no-cost-plan"
        cids = ["ch-a", "ch-b"]
        plan_path = self._write_plan_toml(plan_name, cids)

        records: list[dict] = []
        for cid in cids:
            records.append(self._build_record(
                stage="implement", change_id=cid, plan_name=plan_name,
                duration_ms=50000, input_tokens=30000, output_tokens=6000,
                cost_status="unresolved", estimated_cost=None,
            ))
            records.append(self._build_record(
                stage="review", change_id=cid, plan_name=plan_name,
                duration_ms=20000, input_tokens=15000, output_tokens=3000,
                cost_status="unresolved", estimated_cost=None,
                verdict="pass",
            ))
            records.append(self._build_record(
                stage="archive", change_id=cid, plan_name=plan_name,
                duration_ms=8000, input_tokens=5000, output_tokens=1000,
                cost_status="unresolved", estimated_cost=None,
            ))
        self._write_telemetry(plan_name, records)
        self._write_state(plan_name, {
            "plan": plan_name, "approvals": [],
            "changes": {
                cid: {"status": "done", "round": 1, "phase": "done"}
                for cid in cids
            },
        })

        rc, stdout, _ = self._run_report(plan_path=plan_path)
        self.assertEqual(rc, 0)
        self.assertIn("=== Stage Aggregates ===", stdout)
        # Avg Tokens / Change should show the K suffix, not "—"
        self.assertIn("Avg Tokens / Change:", stdout)
        token_line = next(
            (l for l in stdout.splitlines()
             if "Avg Tokens / Change:" in l),
            "",
        )
        self.assertNotIn("—", token_line,
                         "Avg Tokens / Change must not be '—' when token data is available")
        self.assertIn("K", token_line,
                      "Avg Tokens / Change should show a formatted number, not '—'")
        # Avg Cost / Change should be "—" (no estimated cost)
        cost_line = next(
            (l for l in stdout.splitlines()
             if "Avg Cost / Change:" in l),
            "",
        )
        self.assertIn("—", cost_line,
                      "Avg Cost / Change should be '—' when no estimated cost exists")

        # JSON: avg_tokens must be a number, avg_cost must be null
        rc_json, stdout_json, _ = self._run_report(
            plan_path=plan_path, json=True,
        )
        self.assertEqual(rc_json, 0)
        data = json.loads(stdout_json)
        sa = data["stage_aggregates"]
        self.assertIsNotNone(sa.get("average_tokens_per_change"),
                             "average_tokens_per_change must not be null")
        self.assertIsNone(sa.get("average_cost_per_change"),
                          "average_cost_per_change must be null when no estimated costs")
        self.assertGreater(sa["average_tokens_per_change"], 0)

    # -- regression: all-unresolved plan cost renders as 'unresolved'

    def test_all_unresolved_plan_cost_shows_unresolved(self) -> None:
        """When every cost record in the plan has status 'unresolved',
        the plan summary cost line must render as 'unresolved', not '—'."""
        plan_name = "all-unresolved-plan"
        cid = "ch-unresolved"
        plan_path = self._write_plan_toml(plan_name, [cid])

        records = [
            self._build_record(
                stage="implement", change_id=cid, plan_name=plan_name,
                cost_status="unresolved", estimated_cost=None,
                duration_ms=60000,
            ),
            self._build_record(
                stage="review", change_id=cid, plan_name=plan_name,
                cost_status="unresolved", estimated_cost=None,
                duration_ms=30000, verdict="pass",
            ),
            self._build_record(
                stage="archive", change_id=cid, plan_name=plan_name,
                cost_status="unresolved", estimated_cost=None,
                duration_ms=10000,
            ),
        ]
        self._write_telemetry(plan_name, records)
        self._write_state(plan_name, {
            "plan": plan_name, "approvals": [],
            "changes": {
                cid: {"status": "done", "round": 1, "phase": "done"},
            },
        })

        rc, stdout, _ = self._run_report(plan_path=plan_path)
        self.assertEqual(rc, 0)
        self.assertIn("=== Plan Summary ===", stdout)

        # The cost line should contain "unresolved" not "—"
        cost_line = next(
            (l for l in stdout.splitlines() if l.startswith("Cost:")),
            "",
        )
        self.assertIn("unresolved", cost_line,
                      "All-unresolved plan cost must show 'unresolved', not '—'")
        self.assertNotIn("$", cost_line,
                         "All-unresolved plan cost must not show a dollar amount")


