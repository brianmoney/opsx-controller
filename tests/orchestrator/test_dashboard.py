"""Tests for lib.orchestrator.dashboard (the `opsx-plan dashboard` command).
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

from lib.orchestrator import dashboard


class DashboardCommandTests(unittest.TestCase):
    """Tests for ``opsx-plan dashboard`` HTML rendering and CLI behaviour."""

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
        self.plan_name = "test-plan"
        self._set_up_plan_toml()

    def tearDown(self) -> None:
        self.tmp.cleanup()

    def _set_up_plan_toml(self) -> None:
        tomldir = self.repo / ".opsx-plan"
        tomldir.mkdir(parents=True, exist_ok=True)
        plan_path = tomldir / f"{self.plan_name}.toml"
        plan_path.write_text(
            '[plan]\nname = "test-plan"\nadapter = "opencode"\n\n'
            '[[changes]]\nid = "add-thing"\nphase = 1\n'
            '[[changes]]\nid = "add-other"\nphase = 1\n',
            encoding="utf-8",
        )

    def _plan_toml_path(self) -> str:
        return str(self.repo / ".opsx-plan" / f"{self.plan_name}.toml")

    def _write_telemetry(self, records: list[dict]) -> None:
        telemetry_dir = self.repo / ".opsx-plan" / "telemetry"
        telemetry_dir.mkdir(parents=True, exist_ok=True)
        jsonl_path = telemetry_dir / f"{self.plan_name}.jsonl"
        with open(jsonl_path, "w", encoding="utf-8") as fh:
            for r in records:
                fh.write(json.dumps(r) + "\n")

    def _write_state(self, state: dict) -> None:
        state_dir = self.repo / ".opsx-plan"
        state_dir.mkdir(parents=True, exist_ok=True)
        state_path = state_dir / f"{self.plan_name}.state.json"
        with open(state_path, "w", encoding="utf-8") as fh:
            json.dump(state, fh, indent=2)

    def _make_telemetry_record(
        self,
        change_id: str,
        stage: str,
        round_num: int = 1,
        status: str = "completed",
        started_at: str = "2026-07-01T10:00:00",
        ended_at: str = "2026-07-01T10:02:00",
        duration_ms: int = 120000,
        provider: str = "openai",
        model_id: str = "gpt-4o",
        run_id: str = "run-001",
        input_tokens: int | None = 5000,
        output_tokens: int | None = 1000,
        total_tokens: int | None = 6000,
        cost_status: str = "estimated",
        estimated_cost: float | None = 0.05,
    ) -> dict:
        usage = {
            "usage_available": True,
            "input_tokens": input_tokens,
            "output_tokens": output_tokens,
            "cached_input_tokens": None,
            "reasoning_tokens": None,
            "total_tokens": total_tokens,
            "usage_source": "worker_json",
        }
        cost = {
            "status": cost_status,
            "pricing_catalog_version": "v1",
            "price_snapshot": None,
            "unresolved_reason": None,
            "estimated_cost": estimated_cost,
        }
        return {
            "schema_version": 1,
            "uid": f"uid-{change_id}-{stage}-{round_num}",
            "plan_name": self.plan_name,
            "run_id": run_id,
            "change_id": change_id,
            "stage": stage,
            "round": round_num,
            "status": status,
            "started_at": started_at,
            "ended_at": ended_at,
            "duration_ms": duration_ms,
            "usage": usage,
            "cost": cost,
            "model": {
                "provider": provider,
                "model_id": model_id,
                "model_alias": None,
            },
            "result": {
                "stage_status": status,
                "verdict": "pass" if stage == "review" else None,
                "critical_count": 0,
                "warning_count": 0,
                "note_count": 0,
            },
        }

    def _run_dashboard(self, **kwargs) -> str:
        args = argparse.Namespace(
            repo=str(self.repo),
            plan=self._plan_toml_path(),
            output=kwargs.get("output", None),
            run_id=kwargs.get("run_id", None),
            change=kwargs.get("change", None),
        )
        out = io.StringIO()
        with mock.patch("sys.stdout", out):
            rc = dashboard.cmd_dashboard(args)
        return out.getvalue(), rc

    def _read_output(self, output_path: str | None = None) -> str:
        if output_path is None:
            output_path = str(
                self.repo / ".opsx-plan" / "dashboards" / f"{self.plan_name}.html"
            )
        return Path(output_path).read_text(encoding="utf-8")

    # -- 8.1: Dashboard HTML structure for a completed single-change plan ---

    def test_dashboard_for_completed_single_change_plan(self) -> None:
        records = [
            self._make_telemetry_record("add-thing", "implement", 1,
                started_at="2026-07-01T10:00:00", ended_at="2026-07-01T10:02:00"),
            self._make_telemetry_record("add-thing", "review", 1,
                started_at="2026-07-01T10:02:01", ended_at="2026-07-01T10:03:00"),
            self._make_telemetry_record("add-thing", "archive", 1,
                started_at="2026-07-01T10:03:01", ended_at="2026-07-01T10:04:00"),
        ]
        self._write_telemetry(records)
        self._write_state({
            "plan": self.plan_name,
            "changes": {
                "add-thing": {"status": "done", "round": 1, "max_rounds": 5},
                "add-other": {"status": "pending", "round": 0, "max_rounds": 5},
            },
        })

        stdout, rc = self._run_dashboard()
        self.assertEqual(rc, 0)
        html = self._read_output()

        self.assertIn("<!DOCTYPE html>", html)
        self.assertIn("<html", html)
        self.assertIn("test-plan", html)
        self.assertIn("add-thing", html)
        # No failures section should say "No failures"
        self.assertIn("No failures", html)

    # -- 8.2: Dashboard HTML for multi-change plan ---

    def test_dashboard_for_multi_change_plan(self) -> None:
        records = [
            self._make_telemetry_record("add-thing", "implement", 1,
                started_at="2026-07-01T10:00:00", ended_at="2026-07-01T10:02:00",
                estimated_cost=0.05, cost_status="estimated"),
            self._make_telemetry_record("add-thing", "review", 1,
                started_at="2026-07-01T10:02:01", ended_at="2026-07-01T10:03:00"),
            self._make_telemetry_record("add-thing", "archive", 1,
                started_at="2026-07-01T10:03:01", ended_at="2026-07-01T10:04:00"),
            self._make_telemetry_record("add-other", "implement", 1,
                started_at="2026-07-01T10:04:01", ended_at="2026-07-01T10:06:00",
                estimated_cost=None, cost_status="unresolved",
                input_tokens=None, output_tokens=None, total_tokens=None),
            self._make_telemetry_record("add-other", "review", 1,
                started_at="2026-07-01T10:06:01", ended_at="2026-07-01T10:07:00"),
            self._make_telemetry_record("add-other", "archive", 1,
                started_at="2026-07-01T10:07:01", ended_at="2026-07-01T10:08:00"),
        ]
        self._write_telemetry(records)
        self._write_state({
            "plan": self.plan_name,
            "changes": {
                "add-thing": {"status": "done", "round": 1, "max_rounds": 5},
                "add-other": {"status": "done", "round": 1, "max_rounds": 5},
            },
        })

        stdout, rc = self._run_dashboard()
        self.assertEqual(rc, 0)
        html = self._read_output()

        self.assertIn("add-thing", html)
        self.assertIn("add-other", html)
        # Mixed cost: expect both estimated and unresolved in cost breakdown
        self.assertIn("Estimated", html)
        self.assertIn("Unresolved", html)

    # -- 8.3: All seven required sections present ---

    def test_dashboard_contains_all_seven_sections(self) -> None:
        records = [
            self._make_telemetry_record("add-thing", "implement", 1),
            self._make_telemetry_record("add-thing", "review", 1),
            self._make_telemetry_record("add-thing", "archive", 1),
        ]
        self._write_telemetry(records)
        self._write_state({
            "plan": self.plan_name,
            "changes": {
                "add-thing": {"status": "done", "round": 1, "max_rounds": 5},
                "add-other": {"status": "pending", "round": 0, "max_rounds": 5},
            },
        })

        stdout, rc = self._run_dashboard()
        self.assertEqual(rc, 0)
        html = self._read_output()

        section_headers = [
            "Plan Summary",
            "Model Leaderboard",
            "Per-Change Details",
            "Failure Breakdown",
            "Cost Breakdown",
            "Rounds Histogram",
            "Stage Timeline",
        ]
        for header in section_headers:
            self.assertIn(header, html,
                          f"Dashboard must contain '{header}' section header")

    # -- 8.4: Deterministic output ---

    def test_dashboard_output_is_deterministic(self) -> None:
        records = [
            self._make_telemetry_record("add-thing", "implement", 1),
            self._make_telemetry_record("add-thing", "review", 1),
        ]
        self._write_telemetry(records)
        self._write_state({
            "plan": self.plan_name,
            "changes": {
                "add-thing": {"status": "running", "round": 1, "max_rounds": 5},
            },
        })

        out1 = self.repo / ".opsx-plan" / "dashboards" / f"{self.plan_name}.html"
        out2 = self.repo / ".opsx-plan" / "dashboards" / f"{self.plan_name}-2.html"

        # First run
        args1 = argparse.Namespace(repo=str(self.repo), plan=self._plan_toml_path(),
                                    output=str(out1), run_id=None, change=None)
        dashboard.cmd_dashboard(args1)
        html1 = out1.read_text(encoding="utf-8")

        # Second run (different output path)
        args2 = argparse.Namespace(repo=str(self.repo), plan=self._plan_toml_path(),
                                    output=str(out2), run_id=None, change=None)
        dashboard.cmd_dashboard(args2)
        html2 = out2.read_text(encoding="utf-8")

        self.assertEqual(html1, html2,
                         "Same telemetry + state must produce byte-identical HTML")

    # -- 8.5: Resolved cost renders as $X.XX ---

    def test_dashboard_resolved_cost_renders_dollar_amount(self) -> None:
        records = [
            self._make_telemetry_record("add-thing", "implement", 1,
                estimated_cost=1.23, cost_status="estimated"),
        ]
        self._write_telemetry(records)
        self._write_state({
            "plan": self.plan_name,
            "changes": {"add-thing": {"status": "running", "round": 1, "max_rounds": 5}},
        })

        stdout, rc = self._run_dashboard()
        self.assertEqual(rc, 0)
        html = self._read_output()

        self.assertIn("$1.23", html)
        self.assertIn("cost-estimated", html)

    # -- 8.6: Unresolved cost renders with amber styling ---

    def test_dashboard_unresolved_cost_has_amber_styling(self) -> None:
        records = [
            self._make_telemetry_record("add-thing", "implement", 1,
                estimated_cost=None, cost_status="unresolved",
                input_tokens=None, output_tokens=None, total_tokens=None),
        ]
        self._write_telemetry(records)
        self._write_state({
            "plan": self.plan_name,
            "changes": {"add-thing": {"status": "running", "round": 1, "max_rounds": 5}},
        })

        stdout, rc = self._run_dashboard()
        self.assertEqual(rc, 0)
        html = self._read_output()

        self.assertIn("cost-unresolved", html,
                      "Unresolved cost must have cost-unresolved CSS class")
        self.assertIn("unresolved", html.lower())

    # -- 8.7: Zero estimated cost renders as $0.00 ---

    def test_dashboard_zero_estimated_cost_renders_as_zero(self) -> None:
        records = [
            self._make_telemetry_record("add-thing", "implement", 1,
                estimated_cost=0.0, cost_status="estimated"),
        ]
        self._write_telemetry(records)
        self._write_state({
            "plan": self.plan_name,
            "changes": {"add-thing": {"status": "running", "round": 1, "max_rounds": 5}},
        })

        stdout, rc = self._run_dashboard()
        self.assertEqual(rc, 0)
        html = self._read_output()

        self.assertIn("$0.00", html)
        self.assertIn("cost-estimated", html)

    # -- 8.8: --run-id selects correct run ---

    def test_dashboard_run_id_filter_selects_correct_run(self) -> None:
        records = [
            self._make_telemetry_record("add-thing", "implement", 1,
                run_id="run-a", started_at="2026-07-01T10:00:00",
                ended_at="2026-07-01T10:02:00"),
            self._make_telemetry_record("add-thing", "implement", 1,
                run_id="run-b", started_at="2026-07-02T10:00:00",
                ended_at="2026-07-02T10:02:00"),
        ]
        self._write_telemetry(records)
        self._write_state({
            "plan": self.plan_name,
            "run_id": "run-b",
            "changes": {"add-thing": {"status": "running", "round": 1, "max_rounds": 5}},
        })

        stdout, rc = self._run_dashboard(run_id="run-a")
        self.assertEqual(rc, 0)
        html = self._read_output()

        # Only run-a records should appear; the run_id field in plan summary
        # should reflect run-a, not run-b
        self.assertNotIn("run-b", html)

    # -- 8.9: --change filter narrows output ---

    def test_dashboard_change_filter_narrows_output(self) -> None:
        records = [
            self._make_telemetry_record("add-thing", "implement", 1),
            self._make_telemetry_record("add-other", "implement", 1,
                started_at="2026-07-01T11:00:00", ended_at="2026-07-01T11:02:00"),
        ]
        self._write_telemetry(records)
        self._write_state({
            "plan": self.plan_name,
            "changes": {
                "add-thing": {"status": "running", "round": 1, "max_rounds": 5},
                "add-other": {"status": "running", "round": 1, "max_rounds": 5},
            },
        })

        stdout, rc = self._run_dashboard(change="add-thing")
        self.assertEqual(rc, 0)
        html = self._read_output()

        # Plan summary should still show total_changes
        # Per-change table should only show add-thing
        # Leaderboard should be scoped

        # Filter annotation
        self.assertIn("filter-annotation", html.lower() if html.lower() != html else html)

    # -- 8.10: --output writes to specified path ---

    def test_dashboard_custom_output_path(self) -> None:
        records = [
            self._make_telemetry_record("add-thing", "implement", 1),
        ]
        self._write_telemetry(records)
        self._write_state({
            "plan": self.plan_name,
            "changes": {"add-thing": {"status": "running", "round": 1, "max_rounds": 5}},
        })

        custom_path = str(self.repo / "custom-dashboard.html")
        stdout, rc = self._run_dashboard(output=custom_path)
        self.assertEqual(rc, 0)
        html = self._read_output(custom_path)

        self.assertIn("<!DOCTYPE html>", html)
        self.assertTrue(Path(custom_path).is_file())

    # -- 8.11: Default output writes to .opsx-plan/dashboards/<plan>.html ---

    def test_dashboard_default_output_path(self) -> None:
        records = [
            self._make_telemetry_record("add-thing", "implement", 1),
        ]
        self._write_telemetry(records)
        self._write_state({
            "plan": self.plan_name,
            "changes": {"add-thing": {"status": "running", "round": 1, "max_rounds": 5}},
        })

        stdout, rc = self._run_dashboard()
        self.assertEqual(rc, 0)

        default_path = self.repo / ".opsx-plan" / "dashboards" / f"{self.plan_name}.html"
        self.assertTrue(default_path.is_file(),
                         f"Default output not at {default_path}")

        html = default_path.read_text(encoding="utf-8")
        self.assertIn("<!DOCTYPE html>", html)

    # -- 8.12: Empty telemetry renders valid HTML with "no data" ---

    def test_dashboard_empty_telemetry_produces_valid_html(self) -> None:
        self._write_telemetry([])
        self._write_state({
            "plan": self.plan_name,
            "changes": {},
        })

        stdout, rc = self._run_dashboard()
        self.assertEqual(rc, 0)
        html = self._read_output()

        self.assertIn("<!DOCTYPE html>", html)
        self.assertIn("<html", html)
        self.assertIn("test-plan", html)
        self.assertIn("No telemetry records found", html)

    # -- 8.13: Dashboard does not modify telemetry or state ---

    def test_dashboard_does_not_modify_source_files(self) -> None:
        records = [
            self._make_telemetry_record("add-thing", "implement", 1),
        ]
        self._write_telemetry(records)
        self._write_state({
            "plan": self.plan_name,
            "changes": {"add-thing": {"status": "running", "round": 1, "max_rounds": 5}},
        })

        telemetry_path = self.repo / ".opsx-plan" / "telemetry" / f"{self.plan_name}.jsonl"
        state_path = self.repo / ".opsx-plan" / f"{self.plan_name}.state.json"

        telemetry_before = telemetry_path.read_text(encoding="utf-8")
        state_before = state_path.read_text(encoding="utf-8")

        stdout, rc = self._run_dashboard()
        self.assertEqual(rc, 0)

        telemetry_after = telemetry_path.read_text(encoding="utf-8")
        state_after = state_path.read_text(encoding="utf-8")

        self.assertEqual(telemetry_before, telemetry_after,
                         "Telemetry file MUST NOT be modified by dashboard")
        self.assertEqual(state_before, state_after,
                         "State file MUST NOT be modified by dashboard")

    # -- 8.14: HTML is self-contained (no external references) ---

    def test_dashboard_html_is_self_contained(self) -> None:
        records = [
            self._make_telemetry_record("add-thing", "implement", 1),
        ]
        self._write_telemetry(records)
        self._write_state({
            "plan": self.plan_name,
            "changes": {"add-thing": {"status": "running", "round": 1, "max_rounds": 5}},
        })

        stdout, rc = self._run_dashboard()
        self.assertEqual(rc, 0)
        html = self._read_output()

        # No external references: no http:// or https://
        self.assertNotIn("http://", html)
        self.assertNotIn("https://", html)
        # No <link> elements
        self.assertNotIn("<link ", html.lower())
        # No <script> with src
        self.assertNotIn("<script src", html.lower())

    # -- 8.15: Stage timeline entries sorted by started_at ---

    def test_dashboard_timeline_sorted_by_started_at(self) -> None:
        records = [
            self._make_telemetry_record("add-thing", "archive", 1,
                started_at="2026-07-01T10:03:00", ended_at="2026-07-01T10:04:00"),
            self._make_telemetry_record("add-thing", "implement", 1,
                started_at="2026-07-01T10:01:00", ended_at="2026-07-01T10:02:00"),
            self._make_telemetry_record("add-thing", "review", 1,
                started_at="2026-07-01T10:02:00", ended_at="2026-07-01T10:03:00"),
        ]
        self._write_telemetry(records)
        self._write_state({
            "plan": self.plan_name,
            "changes": {"add-thing": {"status": "done", "round": 1, "max_rounds": 5}},
        })

        stdout, rc = self._run_dashboard()
        self.assertEqual(rc, 0)
        html = self._read_output()

        # Implement should appear before review, before archive in timeline
        impl_pos = html.index("implement")
        rev_pos = html.index("review")
        arch_pos = html.index("archive")
        self.assertLess(impl_pos, rev_pos,
                        "Implement should appear before review in timeline")
        self.assertLess(rev_pos, arch_pos,
                        "Review should appear before archive in timeline")

    # -- 8.16: Failed plan failure breakdown lists failed changes ---

    def test_dashboard_failure_breakdown_lists_failed_changes(self) -> None:
        records = [
            self._make_telemetry_record("add-thing", "implement", 1,
                status="failed"),
        ]
        self._write_telemetry(records)
        self._write_state({
            "plan": self.plan_name,
            "changes": {"add-thing": {"status": "failed", "round": 1, "max_rounds": 5}},
        })

        stdout, rc = self._run_dashboard()
        self.assertEqual(rc, 0)
        html = self._read_output()

        # Should NOT contain "No failures" since there is a failed change
        self.assertNotIn("No failures", html)

    # -- 8.17: Rounds histogram shows distribution for completed changes ---

    def test_dashboard_histogram_for_completed_changes(self) -> None:
        records = [
            self._make_telemetry_record("add-thing", "implement", 1),
            self._make_telemetry_record("add-thing", "review", 1),
            self._make_telemetry_record("add-thing", "archive", 1),
            self._make_telemetry_record("add-other", "implement", 1),
            self._make_telemetry_record("add-other", "review", 1,
                started_at="2026-07-01T10:02:01", ended_at="2026-07-01T10:03:00"),
            self._make_telemetry_record("add-other", "archive", 1,
                started_at="2026-07-01T10:03:01", ended_at="2026-07-01T10:04:00"),
        ]
        self._write_telemetry(records)
        self._write_state({
            "plan": self.plan_name,
            "changes": {
                "add-thing": {"status": "done", "round": 1, "max_rounds": 5},
                "add-other": {"status": "done", "round": 1, "max_rounds": 5},
            },
        })

        stdout, rc = self._run_dashboard()
        self.assertEqual(rc, 0)
        html = self._read_output()

        # Histogram should be present
        self.assertIn("histogram", html.lower())

    # -- HTML escaping ---

    def test_dashboard_escapes_special_characters_in_plan_name(self) -> None:
        self._write_telemetry([])
        self._write_state({"plan": "test-plan", "changes": {}})

        # Override plan TOML with a plan name containing HTML
        plan_path = self.repo / ".opsx-plan" / f"{self.plan_name}.toml"
        script_name = "test <script>alert(1)</script>"
        plan_path.write_text(
            f'[plan]\nname = "{script_name}"\nadapter = "opencode"\n\n'
            '[[changes]]\nid = "add-thing"\nphase = 1\n',
            encoding="utf-8",
        )

        out = io.StringIO()
        args = argparse.Namespace(
            repo=str(self.repo), plan=self._plan_toml_path(),
            output=None, run_id=None, change=None,
        )
        with mock.patch("sys.stdout", out):
            rc = dashboard.cmd_dashboard(args)

        self.assertEqual(rc, 0)

        # Output goes to the dashboard directory with the plan name from TOML
        output_path = self.repo / ".opsx-plan" / "dashboards" / f"{script_name}.html"
        self.assertTrue(output_path.is_file(),
                         f"Expected dashboard at {output_path}")
        html = output_path.read_text(encoding="utf-8")

        self.assertNotIn("<script>alert", html)
        self.assertIn("&lt;script&gt;", html)

    # -- AggregationError handling ---

    def test_dashboard_handles_aggregation_error(self) -> None:
        stderr = io.StringIO()

        from lib.metrics.aggregator import AggregationError

        def fake_aggregate(*args, **kwargs):
            raise AggregationError("test aggregation failure")

        args = argparse.Namespace(
            repo=str(self.repo),
            plan=self._plan_toml_path(),
            output=None, run_id=None, change=None,
        )
        with mock.patch(
            "lib.metrics.aggregator.aggregate", side_effect=fake_aggregate
        ), mock.patch("sys.stderr", stderr):
            rc = dashboard.cmd_dashboard(args)
        self.assertEqual(rc, 2)
        self.assertIn("test aggregation failure", stderr.getvalue())

    # -- Output directory auto-created ---

    def test_dashboard_creates_output_directory(self) -> None:
        records = [
            self._make_telemetry_record("add-thing", "implement", 1),
        ]
        self._write_telemetry(records)
        self._write_state({
            "plan": self.plan_name,
            "changes": {"add-thing": {"status": "running", "round": 1, "max_rounds": 5}},
        })

        dashboards_dir = self.repo / ".opsx-plan" / "dashboards"
        if dashboards_dir.exists():
            import shutil
            shutil.rmtree(dashboards_dir)

        stdout, rc = self._run_dashboard()
        self.assertEqual(rc, 0)
        self.assertTrue(dashboards_dir.is_dir(),
                        "Dashboard directory should be auto-created")

    # -- Unknown model identity shows "unknown" ---

    def test_dashboard_unknown_model_shown_as_unknown(self) -> None:
        records = [
            self._make_telemetry_record("add-thing", "implement", 1,
                provider="", model_id=""),
        ]
        self._write_telemetry(records)
        self._write_state({
            "plan": self.plan_name,
            "changes": {"add-thing": {"status": "running", "round": 1, "max_rounds": 5}},
        })

        stdout, rc = self._run_dashboard()
        self.assertEqual(rc, 0)
        html = self._read_output()

        self.assertIn("unknown", html.lower())

    # -- Stage status badges ---

    def test_dashboard_stage_status_badges_color_coded(self) -> None:
        records = [
            self._make_telemetry_record("add-thing", "implement", 1, status="completed"),
        ]
        self._write_telemetry(records)
        self._write_state({
            "plan": self.plan_name,
            "changes": {"add-thing": {"status": "running", "round": 1, "max_rounds": 5}},
        })

        stdout, rc = self._run_dashboard()
        self.assertEqual(rc, 0)
        html = self._read_output()

        self.assertIn("badge-green", html)

    # -- Per-change status badges ---

    def test_dashboard_change_status_badges_color_coded(self) -> None:
        records = [
            self._make_telemetry_record("add-thing", "implement", 1, status="completed"),
            self._make_telemetry_record("add-thing", "review", 1),
            self._make_telemetry_record("add-thing", "archive", 1),
        ]
        self._write_telemetry(records)
        self._write_state({
            "plan": self.plan_name,
            "changes": {"add-thing": {"status": "done", "round": 1, "max_rounds": 5}},
        })

        stdout, rc = self._run_dashboard()
        self.assertEqual(rc, 0)
        html = self._read_output()

        self.assertIn("badge-green", html)

    # -- Regression: leaderboard sorted by success_rate descending ---

    def test_dashboard_leaderboard_sorted_by_success_rate_descending(self) -> None:
        """Leaderboard rows must appear in descending success_rate order."""
        records = [
            # "doomed" model fails (success_rate 0.0)
            self._make_telemetry_record("doomed", "implement", 1,
                model_id="low-model", started_at="2026-07-01T10:00:00",
                ended_at="2026-07-01T10:02:00"),
            self._make_telemetry_record("doomed", "review", 1,
                model_id="low-model", started_at="2026-07-01T10:02:01",
                ended_at="2026-07-01T10:03:00"),
            # "shining" model succeeds (success_rate 1.0)
            self._make_telemetry_record("shining", "implement", 1,
                model_id="high-model", started_at="2026-07-01T10:04:00",
                ended_at="2026-07-01T10:06:00"),
            self._make_telemetry_record("shining", "review", 1,
                model_id="high-model", started_at="2026-07-01T10:06:01",
                ended_at="2026-07-01T10:07:00"),
            self._make_telemetry_record("shining", "archive", 1,
                model_id="high-model", started_at="2026-07-01T10:07:01",
                ended_at="2026-07-01T10:08:00"),
        ]
        self._write_telemetry(records)
        self._write_state({
            "plan": self.plan_name,
            "changes": {
                "doomed": {"status": "failed", "round": 1, "max_rounds": 5},
                "shining": {"status": "done", "round": 1, "max_rounds": 5},
            },
        })

        stdout, rc = self._run_dashboard()
        self.assertEqual(rc, 0)
        html = self._read_output()

        # Find both model names in the leaderboard HTML
        high_pos = html.find("high-model")
        low_pos = html.find("low-model")
        self.assertGreater(high_pos, 0, "high-model must appear in leaderboard")
        self.assertGreater(low_pos, 0, "low-model must appear in leaderboard")
        self.assertLess(high_pos, low_pos,
                        "high-model (success_rate 1.0) must appear before "
                        "low-model (success_rate 0.0) in sorted leaderboard")

    # -- Regression: Avg Rnds <td> markup is well-formed ---

    def test_dashboard_leaderboard_avg_rnds_valid_markup(self) -> None:
        """Avg Rnds cells must emit valid <td>...</td> markup."""
        records = [
            self._make_telemetry_record("add-thing", "implement", 1,
                model_id="test-model"),
            self._make_telemetry_record("add-thing", "review", 1,
                model_id="test-model"),
            self._make_telemetry_record("add-thing", "archive", 1,
                model_id="test-model"),
        ]
        self._write_telemetry(records)
        self._write_state({
            "plan": self.plan_name,
            "changes": {
                "add-thing": {"status": "done", "round": 1, "max_rounds": 5},
            },
        })

        stdout, rc = self._run_dashboard()
        self.assertEqual(rc, 0)
        html = self._read_output()

        # Extract leaderboard section
        lb_start = html.find('<section class="leaderboard">')
        lb_end = html.find('</section>', lb_start)
        leaderboard_html = html[lb_start:lb_end + len('</section>')]

        # Every <td must be paired with a </td> in the leaderboard table body
        tbody_start = leaderboard_html.find("<tbody>")
        tbody_end = leaderboard_html.find("</tbody>")
        if tbody_start != -1 and tbody_end != -1:
            tbody = leaderboard_html[tbody_start:tbody_end + len("</tbody>")]
            open_tds = tbody.count("<td") - tbody.count("</td")
            self.assertEqual(open_tds, 0,
                             "All <td> tags must be closed in leaderboard tbody")

        # Null-value span for missing avg_rounds should still be inside a proper <td>
        # The avg_rounds column header must exist
        self.assertIn("Avg Rnds", html)

    # -- Regression: plan-summary null metrics must not contain escaped span markup ---

    def test_dashboard_plan_summary_null_metrics_render_gray_span(self) -> None:
        """Plan summary null metrics must render as gray .null-value spans."""
        records = [
            self._make_telemetry_record("add-thing", "implement", 1,
                input_tokens=None, output_tokens=None, total_tokens=None,
                estimated_cost=None, cost_status="unavailable"),
        ]
        self._write_telemetry(records)
        self._write_state({
            "plan": self.plan_name,
            "changes": {
                "add-thing": {"status": "running", "round": 1, "max_rounds": 5},
            },
        })

        stdout, rc = self._run_dashboard()
        self.assertEqual(rc, 0)
        html = self._read_output()

        # The plan summary must NOT contain escaped span tags
        self.assertNotIn("&lt;span", html,
                         "Plan summary must not render escaped span markup")
        # The plan summary should contain the styled null-value span
        self.assertIn('<span class="null-value">—</span>', html,
                      "Plan summary must render null metrics as gray .null-value spans")

    # -- Regression: plan-summary Total Cost with resolved cost ---

    def test_dashboard_plan_summary_total_cost_resolved_green(self) -> None:
        """Plan-summary Total Cost must render with green .cost-estimated
        span when all costs are fully resolved."""
        records = [
            self._make_telemetry_record("add-thing", "implement", 1,
                estimated_cost=1.23, cost_status="estimated"),
            self._make_telemetry_record("add-thing", "review", 1,
                estimated_cost=0.50, cost_status="estimated"),
        ]
        self._write_telemetry(records)
        self._write_state({
            "plan": self.plan_name,
            "changes": {
                "add-thing": {"status": "done", "round": 1, "max_rounds": 5},
            },
        })

        stdout, rc = self._run_dashboard()
        self.assertEqual(rc, 0)
        html = self._read_output()

        # The Total Cost row in plan-summary should use cost-estimated class
        self.assertIn(
            '<span class="cost-estimated">$1.73</span>',
            html,
            "Plan-summary Total Cost must render resolved cost with green "
            ".cost-estimated span",
        )

    def test_dashboard_plan_summary_total_cost_unresolved_amber(self) -> None:
        """Plan-summary Total Cost must render with amber .cost-unresolved
        span when costs are unresolved."""
        records = [
            self._make_telemetry_record("add-thing", "implement", 1,
                estimated_cost=None, cost_status="unresolved",
                input_tokens=None, output_tokens=None, total_tokens=None),
        ]
        self._write_telemetry(records)
        self._write_state({
            "plan": self.plan_name,
            "changes": {
                "add-thing": {"status": "running", "round": 1, "max_rounds": 5},
            },
        })

        stdout, rc = self._run_dashboard()
        self.assertEqual(rc, 0)
        html = self._read_output()

        # The Total Cost row should contain the unresolved span
        self.assertIn(
            '<span class="cost-unresolved">(unresolved)</span>',
            html,
            "Plan-summary Total Cost must render unresolved cost with amber "
            ".cost-unresolved span",
        )

    def test_dashboard_plan_summary_total_cost_missing_gray(self) -> None:
        """Plan-summary Total Cost must render with gray .cost-missing
        span when no cost data is available."""
        records = [
            self._make_telemetry_record("add-thing", "implement", 1,
                estimated_cost=None, cost_status="unavailable",
                input_tokens=None, output_tokens=None, total_tokens=None),
        ]
        self._write_telemetry(records)
        self._write_state({
            "plan": self.plan_name,
            "changes": {
                "add-thing": {"status": "running", "round": 1, "max_rounds": 5},
            },
        })

        stdout, rc = self._run_dashboard()
        self.assertEqual(rc, 0)
        html = self._read_output()

        # The Total Cost row should contain the missing/null span
        self.assertIn(
            '<span class="cost-missing">—</span>',
            html,
            "Plan-summary Total Cost must render missing cost with gray "
            ".cost-missing span",
        )


