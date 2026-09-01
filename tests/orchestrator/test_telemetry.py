from __future__ import annotations

import argparse
import importlib.util
import sys
import io
import json
import os
import re
import shlex
import subprocess
import tempfile
import textwrap
import unittest
import uuid
from pathlib import Path
from unittest import mock

from lib.models import resolver
from lib.models.types import ResolvedModel
from lib.orchestrator import telemetry as telemetry_mod
from lib.orchestrator import state as state_mod
from lib.orchestrator import base as base_mod
from lib.orchestrator import groundtruth as groundtruth_mod
from lib.orchestrator import compiler as compiler_mod

SCRIPT = Path(__file__).resolve().parents[2] / "orchestrator" / "opsx-plan.py"

# Pre-compiled regex for extracting the fenced TOML block emitted by
# build_schema_guidance.
_TOM_BLOCK = re.compile(r"```toml\s*\n(.*?)```", re.DOTALL)

_MODEL_HOME: tempfile.TemporaryDirectory | None = None
_MODEL_CONFIG_PATCH = None
_MODEL_ENV_PATCH = None


def setUpModule() -> None:
    """Pin model resolution so the suite does not read ambient configuration."""
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


class DirectStageTelemetryTests(unittest.TestCase):
    def setUp(self) -> None:
        self.opsx_plan = load_opsx_plan()
        self.tmp = tempfile.TemporaryDirectory()
        self.repo = Path(self.tmp.name)
        # Save and clear env vars so tests start from a known state.
        self._saved_env = {}
        for key in ("OPSX_IMPLEMENTER_MODEL", "OPSX_IMPLEMENTER_ESCALATION_MODEL",
                     "OPSX_REVIEWER_MODEL", "OPSX_ARCHIVER_MODEL",
                     "OPSX_CONTROLLER_MODEL"):
            self._saved_env[key] = os.environ.get(key)
            os.environ.pop(key, None)
        git(self.repo, "init")
        (self.repo / "tracked.txt").write_text("base\n", encoding="utf-8")
        # openspec/changes/archive/ is gitignored here, mirroring this
        # repo: archiving stages nothing, so no archive(<id>): commit is
        # produced and none is required.
        (self.repo / ".gitignore").write_text(
            "openspec/changes/archive/\n", encoding="utf-8"
        )
        git(self.repo, "add", "tracked.txt", ".gitignore")
        git(
            self.repo,
            "-c",
            "user.email=test@example.invalid",
            "-c",
            "user.name=Test User",
            "commit",
            "-m",
            "init",
        )
        self.cid = "add-telemetry-test"
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
        self._saved_checks = groundtruth_mod.run_fast_checks

    def tearDown(self) -> None:
        self.opsx_plan.invoke_direct_stage = self._saved_invoke
        groundtruth_mod.run_fast_checks = self._saved_checks
        # Restore env vars that tests may have modified.
        for key, val in self._saved_env.items():
            if val is not None:
                os.environ[key] = val
            else:
                os.environ.pop(key, None)
        self.tmp.cleanup()

    def write_authored_change(self, cid: str) -> None:
        cdir = self.repo / "openspec" / "changes" / cid
        cdir.mkdir(parents=True)
        (cdir / "proposal.md").write_text("## Why\n", encoding="utf-8")
        (cdir / "tasks.md").write_text(
            "## 1. Tasks\n\n- [ ] 1.1 Example task\n- [ ] 1.2 Example task\n",
            encoding="utf-8",
        )

    def archive_change_in_repo(self, cid: str) -> tuple[str, str]:
        src = self.repo / "openspec" / "changes" / cid
        archive_rel = f"openspec/changes/archive/2026-07-05-{cid}"
        dst = self.repo / archive_rel
        dst.parent.mkdir(parents=True, exist_ok=True)
        src.rename(dst)
        # openspec/changes/archive/ is gitignored: only the change-directory
        # deletion (when tracked) is ever staged, mirroring the real
        # opsx-archiver's git-ls-files guard.
        tracked = subprocess.run(
            ["git", "ls-files", "--", f"openspec/changes/{cid}"],
            cwd=self.repo, check=True, capture_output=True, text=True,
        ).stdout.strip()
        if tracked:
            git(self.repo, "add", "-A", "--", f"openspec/changes/{cid}")
        staged = subprocess.run(
            ["git", "diff", "--cached", "--name-only"],
            cwd=self.repo, check=True, capture_output=True, text=True,
        ).stdout.strip()
        if not staged:
            return archive_rel, ""
        git(
            self.repo,
            "-c",
            "user.email=test@example.invalid",
            "-c",
            "user.name=Test User",
            "commit",
            "-m",
            f"archive({cid}): archive completed OpenSpec change",
        )
        commit = (
            subprocess.run(
                ["git", "rev-parse", "HEAD"],
                cwd=self.repo,
                check=True,
                capture_output=True,
                text=True,
            )
            .stdout.strip()
        )
        return archive_rel, commit

    def stage_runner(self, payloads: list[dict]) -> list[str]:
        input_blocks: list[str] = []

        def fake_invoke(repo, cfg, cid, stage, round_num, input_block):
            self.assertTrue(payloads, f"unexpected stage call: {stage}")
            payload = payloads.pop(0)
            self.assertEqual(stage, payload["stage"])
            input_blocks.append(input_block)
            if stage == "archive" and payload.get("archive_repo"):
                archive_path, commit = self.archive_change_in_repo(cid)
                payload = {
                    **payload,
                    "result": {
                        **payload["result"],
                        "archive_path": archive_path,
                        "commit": commit,
                    },
                    "archive_path": archive_path,
                    "commit": commit,
                }
            log_path = self.opsx_plan.next_stage_log_path(repo, cid, stage, round_num)
            log_path.parent.mkdir(parents=True, exist_ok=True)
            lines = payload.get("lines")
            if lines is None:
                if "result" in payload:
                    body = json.dumps(payload["result"]) + "\n"
                else:
                    body = "\n"
            else:
                body = lines
            if stage == "implement":
                try:
                    parsed = json.loads(body)
                except ValueError:
                    parsed = {}
                if parsed.get("status") == "implemented":
                    tasks_path = repo / "openspec" / "changes" / cid / "tasks.md"
                    if tasks_path.is_file():
                        content = tasks_path.read_text(encoding="utf-8")
                        for tid in parsed.get("completed_tasks") or []:
                            content = re.sub(
                                rf"- \[ \] {re.escape(str(tid))}",
                                "- [x] " + str(tid),
                                content,
                                count=1,
                            )
                        tasks_path.write_text(content, encoding="utf-8")
            log_path.write_text(body, encoding="utf-8")
            return payload.get("outcome", "exited"), log_path

        self.opsx_plan.invoke_direct_stage = fake_invoke
        return input_blocks

    def _read_telemetry(self) -> list[dict]:
        jsonl = self.repo / ".opsx-plan" / "telemetry" / f"{self.plan_name}.jsonl"
        if not jsonl.is_file():
            return []
        records: list[dict] = []
        for line in jsonl.read_text(encoding="utf-8").splitlines():
            if line.strip():
                records.append(json.loads(line))
        return records

    # 6.1
    def test_successful_implement_stage_produces_completed_record(self) -> None:
        self.write_authored_change(self.cid)
        record = state_mod.rec(self.state, self.cid)
        record["max_rounds"] = self.cfg["max_rounds"]
        record["tracked_change_files"] = state_mod.change_context_paths(
            self.repo, self.cid
        )
        self.stage_runner(
            [
                {
                    "stage": "implement",
                    "result": {
                        "status": "implemented",
                        "change": self.cid,
                        "round": 1,
                        "progress_made": True,
                        "completed_tasks": ["1.1", "1.2"],
                        "remaining_tasks": [],
                        "task_counts": {"complete": 2, "total": 2},
                        "files_touched": ["orchestrator/opsx-plan.py"],
                        "known_change_files": [],
                        "summary": "implemented first round",
                    },
                },
                {"stage": "review", "outcome": "timeout"},
            ]
        )

        self.opsx_plan.run_direct_change(self.repo, self.cfg, self.state, self.cid)

        records = self._read_telemetry()
        self.assertGreaterEqual(len(records), 1)
        r = records[0]
        self.assertEqual(r["status"], "completed")
        self.assertEqual(r["stage"], "implement")
        self.assertIsNotNone(r["ended_at"])
        self.assertIsNotNone(r["duration_ms"])
        self.assertGreaterEqual(r["duration_ms"], 0)
        self.assertEqual(r["change_id"], self.cid)
        self.assertEqual(r["plan_name"], self.plan_name)
        self.assertEqual(r["schema_version"], telemetry_mod.TELEMETRY_SCHEMA_VERSION)
        self.assertTrue(r["uid"])
        self.assertIsNotNone(r["started_at"])
        self.assertIn("log_path", r["result"])
        self.assertIsNotNone(r["result"]["log_path"])

    # 7.6: model environment set once (as apply_model_env would) survives the
    # usage-sidecar env restore and is still readable when telemetry
    # attribution re-expands the stage invoke string.
    def test_model_env_still_populated_after_sidecar_restore_for_telemetry(self) -> None:
        saved = os.environ.get("OPSX_IMPLEMENTER_MODEL")
        os.environ["OPSX_IMPLEMENTER_MODEL"] = "deepseek/deepseek-v4-pro"
        try:
            self.cfg["implement_invoke"] = (
                'opencode run --agent opsx-implementer --model "$OPSX_IMPLEMENTER_MODEL"'
            )
            self.write_authored_change(self.cid)
            record = state_mod.rec(self.state, self.cid)
            record["max_rounds"] = self.cfg["max_rounds"]
            record["tracked_change_files"] = state_mod.change_context_paths(
                self.repo, self.cid
            )
            self.stage_runner(
                [
                    {
                        "stage": "implement",
                        "result": {
                            "status": "implemented",
                            "change": self.cid,
                            "round": 1,
                            "progress_made": True,
                            "completed_tasks": ["1.1", "1.2"],
                            "remaining_tasks": [],
                            "task_counts": {"complete": 2, "total": 2},
                            "files_touched": ["orchestrator/opsx-plan.py"],
                            "known_change_files": [],
                            "summary": "implemented first round",
                        },
                    },
                    {"stage": "review", "outcome": "timeout"},
                ]
            )

            self.opsx_plan.run_direct_change(self.repo, self.cfg, self.state, self.cid)

            # The env var must still be set post-run: run_direct_change's
            # usage-sidecar restore only ever touches OPSX_USAGE_PATH and
            # friends, never OPSX_*_MODEL.
            self.assertEqual(os.environ.get("OPSX_IMPLEMENTER_MODEL"), "deepseek/deepseek-v4-pro")

            records = self._read_telemetry()
            self.assertGreaterEqual(len(records), 1)
            r = records[0]
            self.assertEqual(r["stage"], "implement")
            self.assertEqual(r["model"]["provider"], "deepseek")
            self.assertEqual(r["model"]["model_id"], "deepseek-v4-pro")
        finally:
            if saved is not None:
                os.environ["OPSX_IMPLEMENTER_MODEL"] = saved
            else:
                os.environ.pop("OPSX_IMPLEMENTER_MODEL", None)

    # 6.2
    def test_successful_review_stage_populates_verdict_and_findings(self) -> None:
        self.write_authored_change(self.cid)
        record = state_mod.rec(self.state, self.cid)
        record["max_rounds"] = self.cfg["max_rounds"]
        record["tracked_change_files"] = state_mod.change_context_paths(
            self.repo, self.cid
        )
        self.stage_runner(
            [
                {
                    "stage": "implement",
                    "result": {
                        "status": "implemented",
                        "change": self.cid,
                        "round": 1,
                        "progress_made": True,
                        "completed_tasks": ["1.1", "1.2"],
                        "remaining_tasks": [],
                        "task_counts": {"complete": 2, "total": 2},
                        "files_touched": [],
                        "known_change_files": [],
                        "summary": "implemented",
                    },
                },
                {
                    "stage": "review",
                    "result": {
                        "status": "reviewed",
                        "change": self.cid,
                        "round": 1,
                        "verdict": "fail",
                        "finding_counts": {"critical": 2, "warning": 3, "note": 1},
                        "summary": "review failed with findings",
                        "fix_prompt": "fix stuff",
                        "next_phase": "implement",
                    },
                },
                {"stage": "implement", "outcome": "timeout"},
            ]
        )

        self.opsx_plan.run_direct_change(self.repo, self.cfg, self.state, self.cid)

        records = self._read_telemetry()
        review_records = [r for r in records if r["stage"] == "review"]
        self.assertGreaterEqual(len(review_records), 1)
        rev = review_records[0]
        self.assertEqual(rev["status"], "completed")
        self.assertEqual(rev["result"]["verdict"], "fail")
        self.assertEqual(rev["result"]["critical_count"], 2)
        self.assertEqual(rev["result"]["warning_count"], 3)
        self.assertEqual(rev["result"]["note_count"], 1)
        self.assertEqual(rev["result"]["stage_status"], "reviewed")

    # 6.3
    def test_successful_archive_stage_produces_completed_record(self) -> None:
        self.write_authored_change(self.cid)
        record = state_mod.rec(self.state, self.cid)
        record["max_rounds"] = self.cfg["max_rounds"]
        record["tracked_change_files"] = state_mod.change_context_paths(
            self.repo, self.cid
        )
        self.stage_runner(
            [
                {
                    "stage": "implement",
                    "result": {
                        "status": "implemented",
                        "change": self.cid,
                        "round": 1,
                        "progress_made": True,
                        "completed_tasks": ["1.1", "1.2"],
                        "remaining_tasks": [],
                        "task_counts": {"complete": 2, "total": 2},
                        "files_touched": [],
                        "known_change_files": [],
                        "summary": "implemented",
                    },
                },
                {
                    "stage": "review",
                    "result": {
                        "status": "reviewed",
                        "change": self.cid,
                        "round": 1,
                        "verdict": "pass",
                        "finding_counts": {"critical": 0, "warning": 0, "note": 0},
                        "summary": "review passed",
                        "fix_prompt": "",
                        "next_phase": "archive",
                    },
                },
                {
                    "stage": "archive",
                    "archive_repo": True,
                    "result": {
                        "status": "archived",
                        "change": self.cid,
                        "archive_path": "",
                        "spec_sync_status": "no-delta",
                        "commit": "",
                        "summary": "archive succeeded",
                    },
                },
            ]
        )

        self.opsx_plan.run_direct_change(self.repo, self.cfg, self.state, self.cid)

        records = self._read_telemetry()
        archive_records = [r for r in records if r["stage"] == "archive"]
        self.assertGreaterEqual(len(archive_records), 1)
        arch = archive_records[0]
        self.assertEqual(arch["status"], "completed")
        self.assertEqual(arch["result"]["stage_status"], "archived")

    # 6.4
    def test_timeout_produces_timeout_record(self) -> None:
        self.write_authored_change(self.cid)
        record = state_mod.rec(self.state, self.cid)
        record["max_rounds"] = self.cfg["max_rounds"]
        record["tracked_change_files"] = state_mod.change_context_paths(
            self.repo, self.cid
        )
        self.stage_runner(
            [
                {
                    "stage": "implement",
                    "outcome": "timeout",
                },
            ]
        )

        self.opsx_plan.run_direct_change(self.repo, self.cfg, self.state, self.cid)

        records = self._read_telemetry()
        self.assertGreaterEqual(len(records), 1)
        r = records[0]
        self.assertEqual(r["status"], "timeout")
        self.assertIsNotNone(r["result"]["error_message"])
        self.assertIn("timed out", r["result"]["error_message"])
        self.assertIsNotNone(r["duration_ms"])

    # 6.5
    def test_spawn_error_produces_spawn_error_record(self) -> None:
        self.write_authored_change(self.cid)
        record = state_mod.rec(self.state, self.cid)
        record["max_rounds"] = self.cfg["max_rounds"]
        record["tracked_change_files"] = state_mod.change_context_paths(
            self.repo, self.cid
        )
        self.stage_runner(
            [
                {
                    "stage": "implement",
                    "outcome": "spawn_error",
                },
            ]
        )

        self.opsx_plan.run_direct_change(self.repo, self.cfg, self.state, self.cid)

        records = self._read_telemetry()
        self.assertGreaterEqual(len(records), 1)
        r = records[0]
        self.assertEqual(r["status"], "spawn_error")
        self.assertIsNotNone(r["result"]["error_message"])
        self.assertIn("could not spawn", r["result"]["error_message"])
        self.assertIsNone(r["result"]["stage_status"])

    # 6.6
    def test_invalid_worker_json_produces_invalid_output_record(self) -> None:
        self.cfg["invalid_output_retries"] = 0
        self.write_authored_change(self.cid)
        record = state_mod.rec(self.state, self.cid)
        record["max_rounds"] = self.cfg["max_rounds"]
        record["tracked_change_files"] = state_mod.change_context_paths(
            self.repo, self.cid
        )
        self.stage_runner(
            [
                {
                    "stage": "implement",
                    "lines": "not json\nsecond line\n",
                },
            ]
        )

        self.opsx_plan.run_direct_change(self.repo, self.cfg, self.state, self.cid)

        records = self._read_telemetry()
        self.assertGreaterEqual(len(records), 1)
        r = records[0]
        self.assertEqual(r["status"], "invalid_output")
        self.assertIsNotNone(r["result"]["error_message"])
        self.assertIsNone(r["result"]["stage_status"])

    def test_invalid_output_retry_emits_record_per_attempt(self) -> None:
        """Each failed parse writes its own invalid_output telemetry record;
        a recovering retry then records the definitive stage outcome."""
        self.write_authored_change(self.cid)
        record = state_mod.rec(self.state, self.cid)
        record["max_rounds"] = self.cfg["max_rounds"]
        record["tracked_change_files"] = state_mod.change_context_paths(
            self.repo, self.cid
        )
        self.stage_runner(
            [
                {
                    "stage": "implement",
                    "lines": "not json\nsecond line\n",
                },
                {
                    "stage": "implement",
                    "lines": "still prose\n",
                },
                {
                    "stage": "implement",
                    "lines": "prose again\n",
                },
            ]
        )

        result = self.opsx_plan.run_direct_change(self.repo, self.cfg, self.state, self.cid)

        self.assertEqual(result, "failed")
        records = self._read_telemetry()
        invalid = [r for r in records if r["status"] == "invalid_output"]
        # initial attempt + two bounded retries each record the parse failure
        self.assertEqual(len(invalid), 3)

    # 6.7
    def test_telemetry_record_appended_to_correct_plan_jsonl(self) -> None:
        self.write_authored_change(self.cid)
        record = state_mod.rec(self.state, self.cid)
        record["max_rounds"] = self.cfg["max_rounds"]
        record["tracked_change_files"] = state_mod.change_context_paths(
            self.repo, self.cid
        )
        self.stage_runner(
            [
                {
                    "stage": "implement",
                    "result": {
                        "status": "implemented",
                        "change": self.cid,
                        "round": 1,
                        "progress_made": True,
                        "completed_tasks": ["1.1", "1.2"],
                        "remaining_tasks": [],
                        "task_counts": {"complete": 2, "total": 2},
                        "files_touched": [],
                        "known_change_files": [],
                        "summary": "implemented",
                    },
                },
                {"stage": "review", "outcome": "timeout"},
            ]
        )

        self.opsx_plan.run_direct_change(self.repo, self.cfg, self.state, self.cid)

        jsonl = self.repo / ".opsx-plan" / "telemetry" / f"{self.plan_name}.jsonl"
        self.assertTrue(jsonl.is_file(), f"expected {jsonl}")
        content = jsonl.read_text(encoding="utf-8")
        self.assertTrue(content.endswith("\n"))

    # 6.8
    def test_worker_state_includes_telemetry_latest_uid(self) -> None:
        self.write_authored_change(self.cid)
        record = state_mod.rec(self.state, self.cid)
        record["max_rounds"] = self.cfg["max_rounds"]
        record["tracked_change_files"] = state_mod.change_context_paths(
            self.repo, self.cid
        )
        self.stage_runner(
            [
                {
                    "stage": "implement",
                    "result": {
                        "status": "implemented",
                        "change": self.cid,
                        "round": 1,
                        "progress_made": True,
                        "completed_tasks": ["1.1", "1.2"],
                        "remaining_tasks": [],
                        "task_counts": {"complete": 2, "total": 2},
                        "files_touched": [],
                        "known_change_files": [],
                        "summary": "implemented",
                    },
                },
                {"stage": "review", "outcome": "timeout"},
            ]
        )

        self.opsx_plan.run_direct_change(self.repo, self.cfg, self.state, self.cid)

        worker_state = self.opsx_plan.worker_state_path(self.repo, self.plan_name, self.cid)
        self.assertTrue(worker_state.is_file())
        payload = json.loads(worker_state.read_text(encoding="utf-8"))
        self.assertIn("telemetry", payload)
        self.assertIn("latest_telemetry", payload["telemetry"])
        self.assertTrue(payload["telemetry"]["latest_telemetry"])

        # Verify the UID matches what's in the JSONL (latest record)
        records = self._read_telemetry()
        self.assertGreaterEqual(len(records), 1)
        self.assertEqual(
            payload["telemetry"]["latest_telemetry"],
            records[-1]["uid"],
        )

    # 6.9
    def test_usage_and_cost_are_default_unavailable(self) -> None:
        self.write_authored_change(self.cid)
        record = state_mod.rec(self.state, self.cid)
        record["max_rounds"] = self.cfg["max_rounds"]
        record["tracked_change_files"] = state_mod.change_context_paths(
            self.repo, self.cid
        )
        self.stage_runner(
            [
                {
                    "stage": "implement",
                    "result": {
                        "status": "implemented",
                        "change": self.cid,
                        "round": 1,
                        "progress_made": True,
                        "completed_tasks": ["1.1", "1.2"],
                        "remaining_tasks": [],
                        "task_counts": {"complete": 2, "total": 2},
                        "files_touched": [],
                        "known_change_files": [],
                        "summary": "implemented",
                    },
                },
                {"stage": "review", "outcome": "timeout"},
            ]
        )

        self.opsx_plan.run_direct_change(self.repo, self.cfg, self.state, self.cid)

        records = self._read_telemetry()
        r = records[0]
        usage = r["usage"]
        self.assertFalse(usage["usage_available"])
        self.assertIsNone(usage["input_tokens"])
        self.assertIsNone(usage["output_tokens"])
        self.assertIsNone(usage["cached_input_tokens"])
        self.assertIsNone(usage["reasoning_tokens"])
        self.assertIsNone(usage["total_tokens"])
        self.assertIsNone(usage["usage_source"])
        cost = r["cost"]
        self.assertEqual(cost["status"], "unresolved")
        self.assertIsNone(cost["pricing_catalog_version"])
        self.assertIsNone(cost["price_snapshot"])
        self.assertEqual(cost["unresolved_reason"], "usage unavailable")
        self.assertIsNone(cost["estimated_cost"])

    # 6.10
    def test_telemetry_directory_created_on_first_write(self) -> None:
        telemetry_dir = self.repo / ".opsx-plan" / "telemetry"
        self.assertFalse(telemetry_dir.is_dir())

        self.write_authored_change(self.cid)
        record = state_mod.rec(self.state, self.cid)
        record["max_rounds"] = self.cfg["max_rounds"]
        record["tracked_change_files"] = state_mod.change_context_paths(
            self.repo, self.cid
        )
        self.stage_runner(
            [
                {
                    "stage": "implement",
                    "result": {
                        "status": "implemented",
                        "change": self.cid,
                        "round": 1,
                        "progress_made": True,
                        "completed_tasks": ["1.1", "1.2"],
                        "remaining_tasks": [],
                        "task_counts": {"complete": 2, "total": 2},
                        "files_touched": [],
                        "known_change_files": [],
                        "summary": "implemented",
                    },
                },
                {"stage": "review", "outcome": "timeout"},
            ]
        )

        self.opsx_plan.run_direct_change(self.repo, self.cfg, self.state, self.cid)

        self.assertTrue(telemetry_dir.is_dir())

    # 6.11
    def test_run_id_stable_across_pause_and_resume(self) -> None:
        self.write_authored_change(self.cid)
        record = state_mod.rec(self.state, self.cid)
        record["max_rounds"] = self.cfg["max_rounds"]
        record["tracked_change_files"] = state_mod.change_context_paths(
            self.repo, self.cid
        )

        # First run: implement succeeds, review fails
        self.stage_runner(
            [
                {
                    "stage": "implement",
                    "result": {
                        "status": "implemented",
                        "change": self.cid,
                        "round": 1,
                        "progress_made": True,
                        "completed_tasks": ["1.1", "1.2"],
                        "remaining_tasks": [],
                        "task_counts": {"complete": 2, "total": 2},
                        "files_touched": [],
                        "known_change_files": [],
                        "summary": "implemented",
                    },
                },
                {
                    "stage": "review",
                    "result": {
                        "status": "reviewed",
                        "change": self.cid,
                        "round": 1,
                        "verdict": "fail",
                        "finding_counts": {"critical": 1, "warning": 0, "note": 0},
                        "summary": "review failed",
                        "fix_prompt": "fix it",
                        "next_phase": "implement",
                    },
                },
                {
                    "stage": "implement",
                    "outcome": "timeout",
                },
            ]
        )

        self.opsx_plan.run_direct_change(self.repo, self.cfg, self.state, self.cid)

        records = self._read_telemetry()
        self.assertGreaterEqual(len(records), 2)
        run_ids = {r["run_id"] for r in records}
        self.assertEqual(len(run_ids), 1, f"all records should share the same run_id, got: {run_ids}")

        first_run_id = records[0]["run_id"]
        self.assertTrue(first_run_id)

    # 6.12
    def test_existing_resume_behavior_preserved(self) -> None:
        self.write_authored_change(self.cid)
        record = state_mod.rec(self.state, self.cid)
        record["max_rounds"] = self.cfg["max_rounds"]
        record["tracked_change_files"] = state_mod.change_context_paths(
            self.repo, self.cid
        )

        # First run: implement succeeds, review fails
        self.stage_runner(
            [
                {
                    "stage": "implement",
                    "result": {
                        "status": "implemented",
                        "change": self.cid,
                        "round": 1,
                        "progress_made": True,
                        "completed_tasks": ["1.1", "1.2"],
                        "remaining_tasks": [],
                        "task_counts": {"complete": 2, "total": 2},
                        "files_touched": [],
                        "known_change_files": [],
                        "summary": "implemented",
                    },
                },
                {
                    "stage": "review",
                    "result": {
                        "status": "reviewed",
                        "change": self.cid,
                        "round": 1,
                        "verdict": "fail",
                        "finding_counts": {"critical": 1, "warning": 0, "note": 0},
                        "summary": "review failed",
                        "fix_prompt": "Add tests",
                        "next_phase": "implement",
                    },
                },
                {
                    "stage": "implement",
                    "result": {
                        "status": "implemented",
                        "change": self.cid,
                        "round": 2,
                        "progress_made": True,
                        "completed_tasks": ["1.2"],
                        "remaining_tasks": [],
                        "task_counts": {"complete": 2, "total": 2},
                        "files_touched": [],
                        "known_change_files": [],
                        "summary": "implemented round 2",
                    },
                },
                {
                    "stage": "review",
                    "result": {
                        "status": "reviewed",
                        "change": self.cid,
                        "round": 2,
                        "verdict": "pass",
                        "finding_counts": {"critical": 0, "warning": 0, "note": 0},
                        "summary": "review passed",
                        "fix_prompt": "",
                        "next_phase": "archive",
                    },
                },
                {
                    "stage": "archive",
                    "archive_repo": True,
                    "result": {
                        "status": "archived",
                        "change": self.cid,
                        "archive_path": "",
                        "spec_sync_status": "no-delta",
                        "commit": "",
                        "summary": "archive succeeded",
                    },
                },
            ]
        )

        result = self.opsx_plan.run_direct_change(self.repo, self.cfg, self.state, self.cid)

        self.assertEqual(result, base_mod.DONE)
        record = state_mod.rec(self.state, self.cid)
        self.assertEqual(record["phase"], "done")
        self.assertEqual(record["round"], 2)
        self.assertEqual(record["status"], base_mod.DONE)
        self.assertEqual(record["archive"]["status"], "passed")

        # Verify telemetry was written for all stages
        records = self._read_telemetry()
        stages = [r["stage"] for r in records]
        self.assertEqual(stages, ["implement", "review", "implement", "review", "archive"])

    def test_blocked_implement_produces_failed_telemetry(self) -> None:
        self.write_authored_change(self.cid)
        record = state_mod.rec(self.state, self.cid)
        record["max_rounds"] = self.cfg["max_rounds"]
        record["tracked_change_files"] = state_mod.change_context_paths(
            self.repo, self.cid
        )
        self.stage_runner(
            [
                {
                    "stage": "implement",
                    "result": {
                        "status": "blocked",
                        "change": self.cid,
                        "round": 1,
                        "progress_made": False,
                        "completed_tasks": [],
                        "remaining_tasks": ["1.1"],
                        "task_counts": {"complete": 0, "total": 2},
                        "files_touched": [],
                        "known_change_files": [],
                        "summary": "implement blocked",
                        "reason": "missing design artifact",
                    },
                },
            ]
        )

        result = self.opsx_plan.run_direct_change(self.repo, self.cfg, self.state, self.cid)

        self.assertEqual(result, "stop")
        records = self._read_telemetry()
        self.assertGreaterEqual(len(records), 1)
        r = records[0]
        self.assertEqual(r["status"], "failed")
        self.assertEqual(r["stage"], "implement")
        self.assertIsNotNone(r["result"]["error_message"])
        self.assertIn("implement_blocked", r["result"]["error_message"])

    def test_unexpected_implement_status_produces_failed_telemetry(self) -> None:
        self.write_authored_change(self.cid)
        record = state_mod.rec(self.state, self.cid)
        record["max_rounds"] = self.cfg["max_rounds"]
        record["tracked_change_files"] = state_mod.change_context_paths(
            self.repo, self.cid
        )
        self.stage_runner(
            [
                {
                    "stage": "implement",
                    "result": {
                        "status": "unknown-weird-status",
                        "change": self.cid,
                        "round": 1,
                        "progress_made": False,
                        "completed_tasks": [],
                        "remaining_tasks": [],
                        "task_counts": {"complete": 0, "total": 0},
                        "files_touched": [],
                        "known_change_files": [],
                        "summary": "weird",
                    },
                },
            ]
        )

        self.opsx_plan.run_direct_change(self.repo, self.cfg, self.state, self.cid)

        records = self._read_telemetry()
        self.assertGreaterEqual(len(records), 1)
        r = records[0]
        self.assertEqual(r["status"], "failed")
        self.assertIsNotNone(r["result"]["error_message"])
        self.assertIn("implement_invalid", r["result"]["error_message"])

    def test_unexpected_review_status_produces_failed_telemetry(self) -> None:
        self.write_authored_change(self.cid)
        record = state_mod.rec(self.state, self.cid)
        record["max_rounds"] = self.cfg["max_rounds"]
        record["tracked_change_files"] = state_mod.change_context_paths(
            self.repo, self.cid
        )
        self.stage_runner(
            [
                {
                    "stage": "implement",
                    "result": {
                        "status": "implemented",
                        "change": self.cid,
                        "round": 1,
                        "progress_made": True,
                        "completed_tasks": ["1.1", "1.2"],
                        "remaining_tasks": [],
                        "task_counts": {"complete": 2, "total": 2},
                        "files_touched": [],
                        "known_change_files": [],
                        "summary": "implemented",
                    },
                },
                {
                    "stage": "review",
                    "result": {
                        "status": "not-reviewed",
                        "change": self.cid,
                        "round": 1,
                        "verdict": "unknown",
                        "finding_counts": {"critical": 0, "warning": 0, "note": 0},
                        "summary": "bad review",
                        "fix_prompt": "",
                    },
                },
            ]
        )

        self.opsx_plan.run_direct_change(self.repo, self.cfg, self.state, self.cid)

        records = self._read_telemetry()
        review_records = [r for r in records if r["stage"] == "review"]
        self.assertGreaterEqual(len(review_records), 1)
        rev = review_records[0]
        self.assertEqual(rev["status"], "failed")
        self.assertIsNotNone(rev["result"]["error_message"])
        self.assertIn("review_invalid", rev["result"]["error_message"])

    def test_unexpected_review_verdict_produces_failed_telemetry(self) -> None:
        self.write_authored_change(self.cid)
        record = state_mod.rec(self.state, self.cid)
        record["max_rounds"] = self.cfg["max_rounds"]
        record["tracked_change_files"] = state_mod.change_context_paths(
            self.repo, self.cid
        )
        self.stage_runner(
            [
                {
                    "stage": "implement",
                    "result": {
                        "status": "implemented",
                        "change": self.cid,
                        "round": 1,
                        "progress_made": True,
                        "completed_tasks": ["1.1", "1.2"],
                        "remaining_tasks": [],
                        "task_counts": {"complete": 2, "total": 2},
                        "files_touched": [],
                        "known_change_files": [],
                        "summary": "implemented",
                    },
                },
                {
                    "stage": "review",
                    "result": {
                        "status": "reviewed",
                        "change": self.cid,
                        "round": 1,
                        "verdict": "undecided",
                        "finding_counts": {"critical": 0, "warning": 0, "note": 0},
                        "summary": "unexpected verdict",
                        "fix_prompt": "",
                    },
                },
            ]
        )

        self.opsx_plan.run_direct_change(self.repo, self.cfg, self.state, self.cid)

        records = self._read_telemetry()
        review_records = [r for r in records if r["stage"] == "review"]
        self.assertGreaterEqual(len(review_records), 1)
        rev = review_records[0]
        self.assertEqual(rev["status"], "failed")
        self.assertIsNotNone(rev["result"]["error_message"])
        self.assertIn("review_invalid", rev["result"]["error_message"])

    def test_blocked_archive_produces_failed_telemetry(self) -> None:
        self.write_authored_change(self.cid)
        record = state_mod.rec(self.state, self.cid)
        record["max_rounds"] = self.cfg["max_rounds"]
        record["tracked_change_files"] = state_mod.change_context_paths(
            self.repo, self.cid
        )
        self.stage_runner(
            [
                {
                    "stage": "implement",
                    "result": {
                        "status": "implemented",
                        "change": self.cid,
                        "round": 1,
                        "progress_made": True,
                        "completed_tasks": ["1.1", "1.2"],
                        "remaining_tasks": [],
                        "task_counts": {"complete": 2, "total": 2},
                        "files_touched": [],
                        "known_change_files": [],
                        "summary": "implemented",
                    },
                },
                {
                    "stage": "review",
                    "result": {
                        "status": "reviewed",
                        "change": self.cid,
                        "round": 1,
                        "verdict": "pass",
                        "finding_counts": {"critical": 0, "warning": 0, "note": 0},
                        "summary": "review passed",
                        "fix_prompt": "",
                        "next_phase": "archive",
                    },
                },
                {
                    "stage": "archive",
                    "result": {
                        "status": "blocked",
                        "change": self.cid,
                        "archive_path": "",
                        "commit": "",
                        "reason": "cannot archive: dirty tree",
                        "spec_sync_status": "not_started",
                        "summary": "archive blocked",
                    },
                },
            ]
        )

        self.opsx_plan.run_direct_change(self.repo, self.cfg, self.state, self.cid)

        records = self._read_telemetry()
        archive_records = [r for r in records if r["stage"] == "archive"]
        self.assertGreaterEqual(len(archive_records), 1)
        arch = archive_records[0]
        self.assertEqual(arch["status"], "failed")
        self.assertIsNotNone(arch["result"]["error_message"])
        self.assertIn("archive_failed", arch["result"]["error_message"])

    def test_unexpected_archive_status_produces_failed_telemetry(self) -> None:
        self.write_authored_change(self.cid)
        record = state_mod.rec(self.state, self.cid)
        record["max_rounds"] = self.cfg["max_rounds"]
        record["tracked_change_files"] = state_mod.change_context_paths(
            self.repo, self.cid
        )
        self.stage_runner(
            [
                {
                    "stage": "implement",
                    "result": {
                        "status": "implemented",
                        "change": self.cid,
                        "round": 1,
                        "progress_made": True,
                        "completed_tasks": ["1.1", "1.2"],
                        "remaining_tasks": [],
                        "task_counts": {"complete": 2, "total": 2},
                        "files_touched": [],
                        "known_change_files": [],
                        "summary": "implemented",
                    },
                },
                {
                    "stage": "review",
                    "result": {
                        "status": "reviewed",
                        "change": self.cid,
                        "round": 1,
                        "verdict": "pass",
                        "finding_counts": {"critical": 0, "warning": 0, "note": 0},
                        "summary": "review passed",
                        "fix_prompt": "",
                        "next_phase": "archive",
                    },
                },
                {
                    "stage": "archive",
                    "result": {
                        "status": "weird-archive-status",
                        "change": self.cid,
                        "archive_path": "",
                        "commit": "",
                        "reason": "",
                        "spec_sync_status": "not_started",
                        "summary": "unexpected",
                    },
                },
            ]
        )

        self.opsx_plan.run_direct_change(self.repo, self.cfg, self.state, self.cid)

        records = self._read_telemetry()
        archive_records = [r for r in records if r["stage"] == "archive"]
        self.assertGreaterEqual(len(archive_records), 1)
        arch = archive_records[0]
        self.assertEqual(arch["status"], "failed")
        self.assertIsNotNone(arch["result"]["error_message"])
        self.assertIn("archive_invalid", arch["result"]["error_message"])

    def test_telemetry_write_failure_logs_warning_but_does_not_block(self) -> None:
        self.write_authored_change(self.cid)
        record = state_mod.rec(self.state, self.cid)
        record["max_rounds"] = self.cfg["max_rounds"]
        record["tracked_change_files"] = state_mod.change_context_paths(
            self.repo, self.cid
        )
        self.stage_runner(
            [
                {
                    "stage": "implement",
                    "result": {
                        "status": "implemented",
                        "change": self.cid,
                        "round": 1,
                        "progress_made": True,
                        "completed_tasks": ["1.1", "1.2"],
                        "remaining_tasks": [],
                        "task_counts": {"complete": 2, "total": 2},
                        "files_touched": [],
                        "known_change_files": [],
                        "summary": "implemented",
                    },
                },
                {"stage": "review", "outcome": "timeout"},
            ]
        )

        log_calls: list[str] = []

        def capture_log(msg: str) -> None:
            log_calls.append(msg)

        with mock.patch.object(
            telemetry_mod, "write_telemetry_record",
            side_effect=OSError("disk full"),
        ), mock.patch.object(base_mod, "log", side_effect=capture_log):
            self.opsx_plan.run_direct_change(self.repo, self.cfg, self.state, self.cid)

        # Stage must still advance despite telemetry write failure
        record = state_mod.rec(self.state, self.cid)
        self.assertEqual(record["status"], base_mod.FAILED)

        # A warning must have been logged
        warning_msgs = [msg for msg in log_calls if "warning" in msg.lower()]
        self.assertTrue(warning_msgs, f"expected warning log, got: {log_calls}")

    def test_no_progress_produces_failed_telemetry(self) -> None:
        self.write_authored_change(self.cid)
        record = state_mod.rec(self.state, self.cid)
        record["max_rounds"] = self.cfg["max_rounds"]
        record["tracked_change_files"] = state_mod.change_context_paths(
            self.repo, self.cid
        )
        self.stage_runner(
            [
                {
                    "stage": "implement",
                    "result": {
                        "status": "implemented",
                        "change": self.cid,
                        "round": 1,
                        "progress_made": False,
                        "completed_tasks": ["1.1", "1.2"],
                        "remaining_tasks": [],
                        "task_counts": {"complete": 2, "total": 2},
                        "files_touched": [],
                        "known_change_files": [],
                        "summary": "no progress round 1",
                    },
                },
                {
                    "stage": "review",
                    "result": {
                        "status": "reviewed",
                        "change": self.cid,
                        "round": 1,
                        "verdict": "fail",
                        "finding_counts": {"critical": 1, "warning": 0, "note": 0},
                        "summary": "still missing",
                        "fix_prompt": "do it",
                        "next_phase": "implement",
                    },
                },
                {
                    "stage": "implement",
                    "result": {
                        "status": "implemented",
                        "change": self.cid,
                        "round": 2,
                        "progress_made": False,
                        "completed_tasks": [],
                        "remaining_tasks": ["1.1", "1.2"],
                        "task_counts": {"complete": 0, "total": 2},
                        "files_touched": [],
                        "known_change_files": [],
                        "summary": "no progress round 2",
                    },
                },
            ]
        )

        result = self.opsx_plan.run_direct_change(self.repo, self.cfg, self.state, self.cid)

        self.assertEqual(result, "stop")
        records = self._read_telemetry()
        implement_records = [r for r in records if r["stage"] == "implement"]
        # The last implement should have status=failed due to no_progress
        last_impl = implement_records[-1]
        self.assertEqual(last_impl["status"], "failed")
        self.assertIsNotNone(last_impl["result"]["error_message"])
        self.assertIn("no_progress", last_impl["result"]["error_message"])

    def test_max_rounds_produces_failed_telemetry(self) -> None:
        self.write_authored_change(self.cid)
        record = state_mod.rec(self.state, self.cid)
        self.cfg["max_rounds"] = 1
        record["max_rounds"] = 1
        record["tracked_change_files"] = state_mod.change_context_paths(
            self.repo, self.cid
        )
        self.stage_runner(
            [
                {
                    "stage": "implement",
                    "result": {
                        "status": "implemented",
                        "change": self.cid,
                        "round": 1,
                        "progress_made": True,
                        "completed_tasks": ["1.1", "1.2"],
                        "remaining_tasks": [],
                        "task_counts": {"complete": 2, "total": 2},
                        "files_touched": [],
                        "known_change_files": [],
                        "summary": "implemented round 1",
                    },
                },
                {
                    "stage": "review",
                    "result": {
                        "status": "reviewed",
                        "change": self.cid,
                        "round": 1,
                        "verdict": "fail",
                        "finding_counts": {"critical": 1, "warning": 0, "note": 0},
                        "summary": "review failed",
                        "fix_prompt": "fix",
                        "next_phase": "implement",
                    },
                },
            ]
        )

        result = self.opsx_plan.run_direct_change(self.repo, self.cfg, self.state, self.cid)

        self.assertEqual(result, "stop")
        records = self._read_telemetry()
        review_records = [r for r in records if r["stage"] == "review"]
        self.assertGreaterEqual(len(review_records), 1)
        rev = review_records[0]
        self.assertEqual(rev["status"], "failed")
        self.assertIsNotNone(rev["result"]["error_message"])
        self.assertIn("max_rounds_reached", rev["result"]["error_message"])

    def test_review_fail_verdict_continues_and_produces_completed_telemetry(self) -> None:
        """Review with verdict=fail loops back to implement (action=continue),
        so its telemetry must stay 'completed', not 'failed'."""
        self.write_authored_change(self.cid)
        record = state_mod.rec(self.state, self.cid)
        record["max_rounds"] = self.cfg["max_rounds"]
        record["tracked_change_files"] = state_mod.change_context_paths(
            self.repo, self.cid
        )
        self.stage_runner(
            [
                {
                    "stage": "implement",
                    "result": {
                        "status": "implemented",
                        "change": self.cid,
                        "round": 1,
                        "progress_made": True,
                        "completed_tasks": ["1.1", "1.2"],
                        "remaining_tasks": [],
                        "task_counts": {"complete": 2, "total": 2},
                        "files_touched": [],
                        "known_change_files": [],
                        "summary": "implemented",
                    },
                },
                {
                    "stage": "review",
                    "result": {
                        "status": "reviewed",
                        "change": self.cid,
                        "round": 1,
                        "verdict": "fail",
                        "finding_counts": {"critical": 1, "warning": 0, "note": 0},
                        "summary": "review failed",
                        "fix_prompt": "fix it",
                        "next_phase": "implement",
                    },
                },
                {"stage": "implement", "outcome": "timeout"},
            ]
        )

        self.opsx_plan.run_direct_change(self.repo, self.cfg, self.state, self.cid)

        records = self._read_telemetry()
        review_records = [r for r in records if r["stage"] == "review"]
        self.assertGreaterEqual(len(review_records), 1)
        rev = review_records[0]
        self.assertEqual(rev["status"], "completed")
        self.assertEqual(rev["result"]["verdict"], "fail")

    def _assert_worker_state_has_latest_telemetry_uid(self) -> None:
        """Helper: verify worker state JSON has the telemetry UID matching the
        last record in the JSONL file."""
        worker_state = self.opsx_plan.worker_state_path(self.repo, self.plan_name, self.cid)
        self.assertTrue(worker_state.is_file(), f"worker state missing: {worker_state}")
        payload = json.loads(worker_state.read_text(encoding="utf-8"))
        self.assertIn("telemetry", payload)
        self.assertIn("latest_telemetry", payload["telemetry"])
        uid = payload["telemetry"]["latest_telemetry"]
        self.assertTrue(uid, "latest_telemetry must be a non-empty UID string")

        records = self._read_telemetry()
        self.assertGreaterEqual(len(records), 1, "at least one telemetry record expected")
        self.assertEqual(
            uid,
            records[-1]["uid"],
            "worker state telemetry.latest_telemetry must match last JSONL record UID",
        )

    def test_terminal_blocked_implement_persists_telemetry_uid_to_worker_state(self) -> None:
        """Blocked implement (action=stop) must persist its telemetry UID to
        worker state so the link is available for later analysis."""
        self.write_authored_change(self.cid)
        record = state_mod.rec(self.state, self.cid)
        record["max_rounds"] = self.cfg["max_rounds"]
        record["tracked_change_files"] = state_mod.change_context_paths(
            self.repo, self.cid
        )
        self.stage_runner(
            [
                {
                    "stage": "implement",
                    "result": {
                        "status": "blocked",
                        "change": self.cid,
                        "round": 1,
                        "progress_made": False,
                        "completed_tasks": [],
                        "remaining_tasks": ["1.1"],
                        "task_counts": {"complete": 0, "total": 2},
                        "files_touched": [],
                        "known_change_files": [],
                        "summary": "implement blocked",
                        "reason": "missing design artifact",
                    },
                },
            ]
        )

        result = self.opsx_plan.run_direct_change(self.repo, self.cfg, self.state, self.cid)
        self.assertEqual(result, "stop")
        self._assert_worker_state_has_latest_telemetry_uid()

    def test_terminal_blocked_archive_persists_telemetry_uid_to_worker_state(self) -> None:
        """Blocked archive (action=stop) must persist its telemetry UID to
        worker state."""
        self.write_authored_change(self.cid)
        record = state_mod.rec(self.state, self.cid)
        record["max_rounds"] = self.cfg["max_rounds"]
        record["tracked_change_files"] = state_mod.change_context_paths(
            self.repo, self.cid
        )
        self.stage_runner(
            [
                {
                    "stage": "implement",
                    "result": {
                        "status": "implemented",
                        "change": self.cid,
                        "round": 1,
                        "progress_made": True,
                        "completed_tasks": ["1.1", "1.2"],
                        "remaining_tasks": [],
                        "task_counts": {"complete": 2, "total": 2},
                        "files_touched": [],
                        "known_change_files": [],
                        "summary": "implemented",
                    },
                },
                {
                    "stage": "review",
                    "result": {
                        "status": "reviewed",
                        "change": self.cid,
                        "round": 1,
                        "verdict": "pass",
                        "finding_counts": {"critical": 0, "warning": 0, "note": 0},
                        "summary": "review passed",
                        "fix_prompt": "",
                        "next_phase": "archive",
                    },
                },
                {
                    "stage": "archive",
                    "result": {
                        "status": "blocked",
                        "change": self.cid,
                        "archive_path": "",
                        "commit": "",
                        "reason": "cannot archive: dirty tree",
                        "spec_sync_status": "not_started",
                        "summary": "archive blocked",
                    },
                },
            ]
        )

        result = self.opsx_plan.run_direct_change(self.repo, self.cfg, self.state, self.cid)
        self.assertEqual(result, "stop")
        self._assert_worker_state_has_latest_telemetry_uid()

    def test_terminal_successful_archive_persists_telemetry_uid_to_worker_state(self) -> None:
        """Successful archive (action=done) must persist its telemetry UID to
        worker state even though the change is complete."""
        self.write_authored_change(self.cid)
        record = state_mod.rec(self.state, self.cid)
        record["max_rounds"] = self.cfg["max_rounds"]
        record["tracked_change_files"] = state_mod.change_context_paths(
            self.repo, self.cid
        )
        self.stage_runner(
            [
                {
                    "stage": "implement",
                    "result": {
                        "status": "implemented",
                        "change": self.cid,
                        "round": 1,
                        "progress_made": True,
                        "completed_tasks": ["1.1", "1.2"],
                        "remaining_tasks": [],
                        "task_counts": {"complete": 2, "total": 2},
                        "files_touched": [],
                        "known_change_files": [],
                        "summary": "implemented",
                    },
                },
                {
                    "stage": "review",
                    "result": {
                        "status": "reviewed",
                        "change": self.cid,
                        "round": 1,
                        "verdict": "pass",
                        "finding_counts": {"critical": 0, "warning": 0, "note": 0},
                        "summary": "review passed",
                        "fix_prompt": "",
                        "next_phase": "archive",
                    },
                },
                {
                    "stage": "archive",
                    "archive_repo": True,
                    "result": {
                        "status": "archived",
                        "change": self.cid,
                        "archive_path": "",
                        "spec_sync_status": "no-delta",
                        "commit": "",
                        "summary": "archive succeeded",
                    },
                },
            ]
        )

        result = self.opsx_plan.run_direct_change(self.repo, self.cfg, self.state, self.cid)
        self.assertEqual(result, base_mod.DONE)
        self._assert_worker_state_has_latest_telemetry_uid()

    def test_terminal_no_progress_persists_telemetry_uid_to_worker_state(self) -> None:
        """No-progress stop must also persist its telemetry UID."""
        self.write_authored_change(self.cid)
        record = state_mod.rec(self.state, self.cid)
        record["max_rounds"] = self.cfg["max_rounds"]
        record["tracked_change_files"] = state_mod.change_context_paths(
            self.repo, self.cid
        )
        self.stage_runner(
            [
                {
                    "stage": "implement",
                    "result": {
                        "status": "implemented",
                        "change": self.cid,
                        "round": 1,
                        "progress_made": False,
                        "completed_tasks": ["1.1", "1.2"],
                        "remaining_tasks": [],
                        "task_counts": {"complete": 2, "total": 2},
                        "files_touched": [],
                        "known_change_files": [],
                        "summary": "no progress round 1",
                    },
                },
                {
                    "stage": "review",
                    "result": {
                        "status": "reviewed",
                        "change": self.cid,
                        "round": 1,
                        "verdict": "fail",
                        "finding_counts": {"critical": 1, "warning": 0, "note": 0},
                        "summary": "still missing",
                        "fix_prompt": "do it",
                        "next_phase": "implement",
                    },
                },
                {
                    "stage": "implement",
                    "result": {
                        "status": "implemented",
                        "change": self.cid,
                        "round": 2,
                        "progress_made": False,
                        "completed_tasks": [],
                        "remaining_tasks": ["1.1", "1.2"],
                        "task_counts": {"complete": 0, "total": 2},
                        "files_touched": [],
                        "known_change_files": [],
                        "summary": "no progress round 2",
                    },
                },
            ]
        )

        result = self.opsx_plan.run_direct_change(self.repo, self.cfg, self.state, self.cid)
        self.assertEqual(result, "stop")
        self._assert_worker_state_has_latest_telemetry_uid()

    def test_terminal_max_rounds_persists_telemetry_uid_to_worker_state(self) -> None:
        """Max-rounds stop must persist its telemetry UID."""
        self.write_authored_change(self.cid)
        record = state_mod.rec(self.state, self.cid)
        self.cfg["max_rounds"] = 1
        record["max_rounds"] = 1
        record["tracked_change_files"] = state_mod.change_context_paths(
            self.repo, self.cid
        )
        self.stage_runner(
            [
                {
                    "stage": "implement",
                    "result": {
                        "status": "implemented",
                        "change": self.cid,
                        "round": 1,
                        "progress_made": True,
                        "completed_tasks": [],
                        "remaining_tasks": ["1.1"],
                        "task_counts": {"complete": 0, "total": 2},
                        "files_touched": [],
                        "known_change_files": [],
                        "summary": "implemented round 1",
                    },
                },
                {
                    "stage": "review",
                    "result": {
                        "status": "reviewed",
                        "change": self.cid,
                        "round": 1,
                        "verdict": "fail",
                        "finding_counts": {"critical": 1, "warning": 0, "note": 0},
                        "summary": "review failed",
                        "fix_prompt": "fix",
                        "next_phase": "implement",
                    },
                },
            ]
        )

        result = self.opsx_plan.run_direct_change(self.repo, self.cfg, self.state, self.cid)
        self.assertEqual(result, "stop")
        self._assert_worker_state_has_latest_telemetry_uid()

    def test_terminal_unexpected_archive_verdict_persists_telemetry_uid_to_worker_state(self) -> None:
        """Unexpected archive verdict (action=stop) must also persist its
        telemetry UID."""
        self.write_authored_change(self.cid)
        record = state_mod.rec(self.state, self.cid)
        record["max_rounds"] = self.cfg["max_rounds"]
        record["tracked_change_files"] = state_mod.change_context_paths(
            self.repo, self.cid
        )
        self.stage_runner(
            [
                {
                    "stage": "implement",
                    "result": {
                        "status": "implemented",
                        "change": self.cid,
                        "round": 1,
                        "progress_made": True,
                        "completed_tasks": ["1.1", "1.2"],
                        "remaining_tasks": [],
                        "task_counts": {"complete": 2, "total": 2},
                        "files_touched": [],
                        "known_change_files": [],
                        "summary": "implemented",
                    },
                },
                {
                    "stage": "review",
                    "result": {
                        "status": "reviewed",
                        "change": self.cid,
                        "round": 1,
                        "verdict": "pass",
                        "finding_counts": {"critical": 0, "warning": 0, "note": 0},
                        "summary": "review passed",
                        "fix_prompt": "",
                        "next_phase": "archive",
                    },
                },
                {
                    "stage": "archive",
                    "result": {
                        "status": "weird-archive-status",
                        "change": self.cid,
                        "archive_path": "",
                        "commit": "",
                        "reason": "",
                        "spec_sync_status": "not_started",
                        "summary": "unexpected",
                    },
                },
            ]
        )

        result = self.opsx_plan.run_direct_change(self.repo, self.cfg, self.state, self.cid)
        self.assertEqual(result, "stop")
        self._assert_worker_state_has_latest_telemetry_uid()

    def test_escalated_implement_writes_escalation_model_in_telemetry(self) -> None:
        """4.6: escalated implement round writes escalation model in telemetry"""
        self.write_authored_change(self.cid)
        impl_model = "deepseek/deepseek-v4-basic"
        esc_model = "deepseek/deepseek-v4-ultra"
        os.environ["OPSX_IMPLEMENTER_MODEL"] = impl_model
        os.environ["OPSX_IMPLEMENTER_ESCALATION_MODEL"] = esc_model
        os.environ["OPSX_REVIEWER_MODEL"] = "github-copilot/gpt-5.4"
        os.environ["OPSX_ARCHIVER_MODEL"] = "github-copilot/gpt-5.4"

        self.cfg["escalate_after_review_fails"] = 1
        self.cfg["implement_invoke"] = (
            'opencode run --agent opsx-implementer --model "$OPSX_IMPLEMENTER_MODEL"'
        )
        self.cfg["max_rounds"] = 2

        record = state_mod.rec(self.state, self.cid)
        record["max_rounds"] = self.cfg["max_rounds"]
        record["tracked_change_files"] = state_mod.change_context_paths(
            self.repo, self.cid
        )
        self.stage_runner(
            [
                {
                    "stage": "implement",
                    "result": {
                        "status": "implemented",
                        "change": self.cid,
                        "round": 1,
                        "progress_made": True,
                        "completed_tasks": ["1.1", "1.2"],
                        "remaining_tasks": [],
                        "task_counts": {"complete": 2, "total": 2},
                        "files_touched": ["orchestrator/opsx-plan.py"],
                        "known_change_files": [],
                        "summary": "r1",
                    },
                },
                {
                    "stage": "review",
                    "result": {
                        "status": "reviewed",
                        "change": self.cid,
                        "round": 1,
                        "verdict": "fail",
                        "finding_counts": {"critical": 1, "warning": 0, "note": 0},
                        "summary": "review failed",
                        "fix_prompt": "fix",
                    },
                },
                {
                    "stage": "implement",
                    "result": {
                        "status": "implemented",
                        "change": self.cid,
                        "round": 2,
                        "progress_made": True,
                        "completed_tasks": ["1.2"],
                        "remaining_tasks": [],
                        "task_counts": {"complete": 2, "total": 2},
                        "files_touched": ["orchestrator/opsx-plan.py"],
                        "known_change_files": [],
                        "summary": "r2 escalated",
                    },
                },
                {
                    "stage": "review",
                    "result": {
                        "status": "reviewed",
                        "change": self.cid,
                        "round": 2,
                        "verdict": "pass",
                        "finding_counts": {"critical": 0, "warning": 0, "note": 0},
                        "summary": "pass",
                        "fix_prompt": "",
                    },
                },
                {
                    "stage": "archive",
                    "archive_repo": True,
                    "result": {
                        "status": "archived",
                        "change": self.cid,
                        "round": 2,
                        "verdict": "pass",
                        "finding_counts": {"critical": 0, "warning": 0, "note": 0},
                        "summary": "archived",
                        "fix_prompt": "",
                    },
                },
            ]
        )

        result = self.opsx_plan.run_direct_change(self.repo, self.cfg, self.state, self.cid)
        self.assertEqual(result, "done")

        records = self._read_telemetry()
        implement_records = [r for r in records if r["stage"] == "implement"]
        self.assertEqual(len(implement_records), 2)

        # Round 1: base model (provider prefix stripped by extraction)
        r1_model = implement_records[0].get("model", {})
        self.assertEqual(r1_model.get("model_id"), "deepseek-v4-basic",
                         "round 1 telemetry must show base model")

        # Round 2: escalated model (provider prefix stripped by extraction)
        r2_model = implement_records[1].get("model", {})
        self.assertEqual(r2_model.get("model_id"), "deepseek-v4-ultra",
                         "round 2 telemetry must show escalation model")


class DirectStageUsageExtractionTests(unittest.TestCase):
    """Unit tests for usage / model metadata extraction functions."""

    def setUp(self) -> None:
        self.opsx_plan = load_opsx_plan()
        self.tmp = tempfile.TemporaryDirectory()
        self.log_dir = Path(self.tmp.name)

    def tearDown(self) -> None:
        self.tmp.cleanup()

    def _write_log(self, *lines: str) -> Path:
        p = self.log_dir / f"test-{hash(lines)}.log"
        p.write_text("\n".join(lines) + "\n", encoding="utf-8")
        return p

    # -- Extraction helpers -------------------------------------------------

    def _assert_usage_unavailable(self, usage: dict) -> None:
        self.assertFalse(usage["usage_available"])
        self.assertIsNone(usage["usage_source"])
        self.assertIsNone(usage["input_tokens"])
        self.assertIsNone(usage["output_tokens"])
        self.assertIsNone(usage["cached_input_tokens"])
        self.assertIsNone(usage["reasoning_tokens"])
        self.assertIsNone(usage["total_tokens"])

    def _assert_model_null(self, model: dict) -> None:
        self.assertIsNone(model["provider"])
        self.assertIsNone(model["model_id"])
        self.assertIsNone(model["model_alias"])

    # -- 4.1 Full token usage from worker JSON ------------------------------

    def test_worker_json_full_usage_populates_all_fields(self) -> None:
        payload = {
            "status": "implemented",
            "change": "ex",
            "round": 1,
            "usage": {
                "input_tokens": 100,
                "output_tokens": 20,
                "cached_input_tokens": 10,
                "reasoning_tokens": 5,
                "total_tokens": 135,
            },
        }
        usage, model = telemetry_mod.extract_usage_and_model(payload, None)
        self.assertTrue(usage["usage_available"])
        self.assertEqual(usage["usage_source"], "worker_json")
        self.assertEqual(usage["input_tokens"], 100)
        self.assertEqual(usage["output_tokens"], 20)
        self.assertEqual(usage["cached_input_tokens"], 10)
        self.assertEqual(usage["reasoning_tokens"], 5)
        self.assertEqual(usage["total_tokens"], 135)

    def test_worker_json_full_usage_top_level_alternate_keys(self) -> None:
        payload = {
            "status": "implemented",
            "change": "ex",
            "prompt_tokens": 100,
            "completion_tokens": 20,
            "cachedInputTokens": 10,
            "reasoningTokens": 5,
            "totalTokens": 135,
        }
        usage, _ = telemetry_mod.extract_usage_and_model(payload, None)
        self.assertTrue(usage["usage_available"])
        self.assertEqual(usage["usage_source"], "worker_json")
        self.assertEqual(usage["input_tokens"], 100)
        self.assertEqual(usage["output_tokens"], 20)
        self.assertEqual(usage["cached_input_tokens"], 10)
        self.assertEqual(usage["reasoning_tokens"], 5)
        self.assertEqual(usage["total_tokens"], 135)

    # -- 4.2 Partial token usage --------------------------------------------

    def test_worker_json_partial_usage_preserves_null(self) -> None:
        payload = {
            "status": "implemented",
            "change": "ex",
            "usage": {
                "input_tokens": 100,
                "output_tokens": 20,
            },
        }
        usage, _ = telemetry_mod.extract_usage_and_model(payload, None)
        self.assertTrue(usage["usage_available"])
        self.assertEqual(usage["usage_source"], "worker_json")
        self.assertEqual(usage["input_tokens"], 100)
        self.assertEqual(usage["output_tokens"], 20)
        self.assertIsNone(usage["cached_input_tokens"])
        self.assertIsNone(usage["reasoning_tokens"])
        self.assertIsNone(usage["total_tokens"])

    # -- 4.3 Zero token values ---------------------------------------------

    def test_reported_zero_token_values_remain_zero(self) -> None:
        payload = {
            "status": "implemented",
            "change": "ex",
            "usage": {
                "input_tokens": 0,
                "output_tokens": 0,
                "total_tokens": 0,
            },
        }
        usage, _ = telemetry_mod.extract_usage_and_model(payload, None)
        self.assertTrue(usage["usage_available"])
        self.assertEqual(usage["input_tokens"], 0)
        self.assertEqual(usage["output_tokens"], 0)
        self.assertEqual(usage["total_tokens"], 0)
        self.assertIsNone(usage["cached_input_tokens"])
        self.assertIsNone(usage["reasoning_tokens"])

    # -- 4.4 Model metadata from worker JSON -------------------------------

    def test_worker_json_model_identity_populates_fields(self) -> None:
        payload = {
            "status": "implemented",
            "change": "ex",
            "model": {
                "provider": "openai",
                "model_id": "gpt-5.5",
                "model_alias": "primary",
            },
        }
        _, model = telemetry_mod.extract_usage_and_model(payload, None)
        self.assertEqual(model["provider"], "openai")
        self.assertEqual(model["model_id"], "gpt-5.5")
        self.assertEqual(model["model_alias"], "primary")
        self.assertEqual(model["attribution"], "observed")

    def test_worker_json_model_top_level_alternate_keys(self) -> None:
        payload = {
            "status": "implemented",
            "change": "ex",
            "provider": "anthropic",
            "modelId": "claude-4",
        }
        _, model = telemetry_mod.extract_usage_and_model(payload, None)
        self.assertEqual(model["provider"], "anthropic")
        self.assertEqual(model["model_id"], "claude-4")
        self.assertIsNone(model["model_alias"])

    # -- 4.5 Log metadata fallback -----------------------------------------

    def test_log_metadata_fallback_usage_when_worker_json_has_none(self) -> None:
        log_path = self._write_log(
            "# header",
            '{"input_tokens": 200, "output_tokens": 50}',
        )
        # Worker JSON has no usage fields
        payload = {"status": "implemented", "change": "ex"}
        usage, _ = telemetry_mod.extract_usage_and_model(payload, log_path)
        self.assertTrue(usage["usage_available"])
        self.assertEqual(usage["usage_source"], "log_metadata")
        self.assertEqual(usage["input_tokens"], 200)
        self.assertEqual(usage["output_tokens"], 50)

    def test_log_metadata_fallback_model_when_worker_json_has_none(self) -> None:
        log_path = self._write_log(
            "# header",
            '{"provider": "openai", "model_id": "gpt-5.5"}',
        )
        payload = {"status": "implemented", "change": "ex"}
        _, model = telemetry_mod.extract_usage_and_model(payload, log_path)
        self.assertEqual(model["provider"], "openai")
        self.assertEqual(model["model_id"], "gpt-5.5")
        self.assertEqual(model["attribution"], "observed")

    # -- 4.6 Worker JSON takes precedence over log -------------------------

    def test_worker_json_usage_wins_over_log_metadata(self) -> None:
        log_path = self._write_log(
            '{"input_tokens": 999, "output_tokens": 888}',
        )
        payload = {
            "status": "implemented",
            "change": "ex",
            "usage": {"input_tokens": 100, "output_tokens": 20},
        }
        usage, _ = telemetry_mod.extract_usage_and_model(payload, log_path)
        self.assertTrue(usage["usage_available"])
        self.assertEqual(usage["usage_source"], "worker_json")
        self.assertEqual(usage["input_tokens"], 100)
        self.assertEqual(usage["output_tokens"], 20)

    def test_worker_json_model_wins_over_log_metadata(self) -> None:
        log_path = self._write_log(
            '{"provider": "log-provider", "model_id": "log-model"}',
        )
        payload = {
            "status": "implemented",
            "change": "ex",
            "model": {"provider": "worker-provider", "model_id": "worker-model"},
        }
        _, model = telemetry_mod.extract_usage_and_model(payload, log_path)
        self.assertEqual(model["provider"], "worker-provider")
        self.assertEqual(model["model_id"], "worker-model")

    def test_log_model_fallback_blocked_when_worker_has_any_model_field(self) -> None:
        """Worker provides provider only; log model fallback is blocked because worker already carries a model field."""
        log_path = self._write_log(
            '{"model_id": "log-model-id"}',
        )
        payload = {"status": "implemented", "change": "ex", "provider": "openai"}
        _, model = telemetry_mod.extract_usage_and_model(payload, log_path)
        self.assertEqual(model["provider"], "openai")
        self.assertIsNone(model["model_id"])

    def test_log_model_fallback_when_worker_has_no_model(self) -> None:
        """Log provides model identity when worker JSON has none."""
        log_path = self._write_log(
            '{"model_id": "log-model-id", "provider": "log-provider"}',
        )
        payload = {"status": "implemented", "change": "ex"}
        _, model = telemetry_mod.extract_usage_and_model(payload, log_path)
        self.assertEqual(model["provider"], "log-provider")
        self.assertEqual(model["model_id"], "log-model-id")

    def test_invocation_model_fallback_reads_installed_agent_model(self) -> None:
        home_dir = self.log_dir / "home"
        agent_dir = home_dir / ".config" / "opencode" / "agents"
        agent_dir.mkdir(parents=True)
        (agent_dir / "opsx-reviewer.md").write_text(
            "---\nmodel: \"openai/gpt-5.4\"\n---\n",
            encoding="utf-8",
        )

        with mock.patch.dict(os.environ, {"HOME": str(home_dir)}, clear=False):
            model = telemetry_mod._extract_invocation_model(
                "opencode run --agent opsx-reviewer"
            )

        self.assertEqual(model["provider"], "openai")
        self.assertEqual(model["model_id"], "gpt-5.4")
        self.assertIsNone(model["model_alias"])

    def test_invocation_model_resolution_unchanged_for_opencode_with_explicit_adapter(self) -> None:
        home_dir = self.log_dir / "home-opencode"
        agent_dir = home_dir / ".config" / "opencode" / "agents"
        agent_dir.mkdir(parents=True)
        (agent_dir / "opsx-reviewer.md").write_text(
            "---\nmodel: \"openai/gpt-5.4\"\n---\n",
            encoding="utf-8",
        )

        with mock.patch.dict(os.environ, {"HOME": str(home_dir)}, clear=False):
            model = telemetry_mod._extract_invocation_model(
                "opencode run --agent opsx-reviewer", "opencode"
            )

        self.assertEqual(model["provider"], "openai")
        self.assertEqual(model["model_id"], "gpt-5.4")

    def test_invocation_model_resolved_from_claude_code_agent_directory(self) -> None:
        home_dir = self.log_dir / "home-claude"
        agent_dir = home_dir / ".claude" / "agents"
        agent_dir.mkdir(parents=True)
        (agent_dir / "opsx-implementer.md").write_text(
            "---\nmodel: \"anthropic/claude-opus-5\"\n---\n",
            encoding="utf-8",
        )

        with mock.patch.dict(os.environ, {"HOME": str(home_dir)}, clear=False):
            model = telemetry_mod._extract_invocation_model(
                "claude -p --agent opsx-implementer", "claude-code"
            )

        self.assertEqual(model["provider"], "anthropic")
        self.assertEqual(model["model_id"], "claude-opus-5")
        self.assertIsNone(model["model_alias"])

    def test_invocation_model_claude_code_does_not_read_opencode_agent_dir(self) -> None:
        """A claude-code invocation must not resolve against the opencode
        agent directory even if a same-named agent file exists there."""
        home_dir = self.log_dir / "home-mixed"
        opencode_agent_dir = home_dir / ".config" / "opencode" / "agents"
        opencode_agent_dir.mkdir(parents=True)
        (opencode_agent_dir / "opsx-implementer.md").write_text(
            "---\nmodel: \"openai/gpt-5.4\"\n---\n",
            encoding="utf-8",
        )
        # No .claude/agents directory exists under this fake home.

        with mock.patch.dict(os.environ, {"HOME": str(home_dir)}, clear=False):
            model = telemetry_mod._extract_invocation_model(
                "claude -p --agent opsx-implementer", "claude-code"
            )

        self.assertIsNone(model["provider"])
        self.assertIsNone(model["model_id"])

    def test_invocation_model_resolved_from_repo_local_claude_code_agent_directory(self) -> None:
        home_dir = self.log_dir / "home-claude-repo-fallback"
        home_dir.mkdir(parents=True, exist_ok=True)
        repo_dir = self.log_dir / "repo-claude"
        agent_dir = repo_dir / ".claude" / "agents"
        agent_dir.mkdir(parents=True)
        (agent_dir / "opsx-implementer.md").write_text(
            "---\nmodel: \"anthropic/claude-opus-5\"\n---\n",
            encoding="utf-8",
        )
        # No agent file at all under the fake home; only the repo-local install has it.

        with mock.patch.dict(os.environ, {"HOME": str(home_dir)}, clear=False):
            model = telemetry_mod._extract_invocation_model(
                "claude -p --agent opsx-implementer", "claude-code", repo_dir
            )

        self.assertEqual(model["provider"], "anthropic")
        self.assertEqual(model["model_id"], "claude-opus-5")
        self.assertIsNone(model["model_alias"])

    def test_invocation_model_fallback_expands_env_var_model_flag(self) -> None:
        """Regression test: the invocation-model fallback must resolve the
        model that was actually dispatched, not the unexpanded $VAR
        placeholder still stored in the plan config's invoke string."""
        var = "OPSX_TEST_TELEMETRY_MODEL"
        os.environ[var] = "sonnet"
        try:
            expanded = telemetry_mod._best_effort_expand_invoke(
                f'claude -p --agent opsx-implementer --model "${var}"'
            )
            model = telemetry_mod._extract_invocation_model(expanded, "claude-code")
        finally:
            del os.environ[var]

        self.assertEqual(model["model_id"], "sonnet")
        self.assertIsNone(model["provider"])

    def test_best_effort_expand_invoke_leaves_unset_var_unexpanded(self) -> None:
        """Best-effort expansion must not raise or fail-closed like the
        dispatch-time expansion — it just leaves an unset reference as-is."""
        var = "OPSX_TEST_TELEMETRY_MODEL_UNSET"
        os.environ.pop(var, None)
        expanded = telemetry_mod._best_effort_expand_invoke(
            f'claude -p --model "${var}"'
        )
        self.assertIn(f"${var}", expanded)

    # -- 4.7 Unknown formats -----------------------------------------------

    def test_unknown_format_produces_unavailable_usage(self) -> None:
        payload = {"status": "implemented", "change": "ex"}
        usage, model = telemetry_mod.extract_usage_and_model(payload, None)
        self._assert_usage_unavailable(usage)
        self._assert_model_null(model)

    def test_log_without_usage_produces_unavailable(self) -> None:
        log_path = self._write_log(
            "# just some text",
            "no json here",
            '{"some_other_field": 42}',
        )
        payload = {"status": "implemented", "change": "ex"}
        usage, _ = telemetry_mod.extract_usage_and_model(payload, log_path)
        self._assert_usage_unavailable(usage)

    def test_none_payload_produces_unavailable(self) -> None:
        usage, model = telemetry_mod.extract_usage_and_model(None, None)
        self._assert_usage_unavailable(usage)
        self._assert_model_null(model)

    # -- 4.8 Malformed usage values ----------------------------------------

    def test_negative_token_value_ignored(self) -> None:
        payload = {
            "status": "implemented",
            "change": "ex",
            "usage": {"input_tokens": -1, "output_tokens": 20},
        }
        usage, _ = telemetry_mod.extract_usage_and_model(payload, None)
        self.assertTrue(usage["usage_available"])
        self.assertIsNone(usage["input_tokens"])
        self.assertEqual(usage["output_tokens"], 20)

    def test_floating_point_token_value_ignored(self) -> None:
        payload = {
            "status": "implemented",
            "change": "ex",
            "usage": {"input_tokens": 100.5},
        }
        usage, _ = telemetry_mod.extract_usage_and_model(payload, None)
        self._assert_usage_unavailable(usage)

    def test_non_numeric_token_value_ignored(self) -> None:
        payload = {
            "status": "implemented",
            "change": "ex",
            "usage": {"input_tokens": "100", "total_tokens": None},
        }
        usage, _ = telemetry_mod.extract_usage_and_model(payload, None)
        self._assert_usage_unavailable(usage)

    def test_boolean_token_value_ignored(self) -> None:
        payload = {
            "status": "implemented",
            "change": "ex",
            "usage": {"input_tokens": True, "output_tokens": False},
        }
        usage, _ = telemetry_mod.extract_usage_and_model(payload, None)
        self._assert_usage_unavailable(usage)

    # -- 4.9 Default-unavailable for failure outcomes -----------------------

    def test_timeout_record_usage_default_unavailable(self) -> None:
        """Simulate payload=None (timeout path) keeps usage unavailable."""
        usage, model = telemetry_mod.extract_usage_and_model(None, None)
        self._assert_usage_unavailable(usage)
        self._assert_model_null(model)

    def test_spawn_error_record_usage_default_unavailable(self) -> None:
        usage, model = telemetry_mod.extract_usage_and_model(None, None)
        self._assert_usage_unavailable(usage)
        self._assert_model_null(model)

    def test_invalid_output_record_usage_default_unavailable(self) -> None:
        usage, model = telemetry_mod.extract_usage_and_model(None, None)
        self._assert_usage_unavailable(usage)
        self._assert_model_null(model)

    # -- Edge cases ---------------------------------------------------------

    def test_extraction_never_raises_on_broken_payload(self) -> None:
        """Extraction must be best-effort and never raise."""
        # A payload that is a dict but has weird internal types should not
        # crash the extractor.
        payload = {"usage": "not_a_dict"}
        usage, model = telemetry_mod.extract_usage_and_model(payload, None)
        self._assert_usage_unavailable(usage)
        self._assert_model_null(model)

    def test_log_with_malformed_json_lines_is_ignored(self) -> None:
        log_path = self._write_log(
            "{not valid json",
            '{"input_tokens": 50}',
            "{still not valid",
        )
        payload = {"status": "implemented", "change": "ex"}
        usage, _ = telemetry_mod.extract_usage_and_model(payload, log_path)
        self.assertTrue(usage["usage_available"])
        self.assertEqual(usage["usage_source"], "log_metadata")
        self.assertEqual(usage["input_tokens"], 50)

    def test_nested_usage_object_recognized(self) -> None:
        payload = {
            "status": "implemented",
            "change": "ex",
            "usage": {
                "input_tokens": 500,
                "output_tokens": 200,
            },
        }
        usage, _ = telemetry_mod.extract_usage_and_model(payload, None)
        self.assertTrue(usage["usage_available"])
        self.assertEqual(usage["usage_source"], "worker_json")
        self.assertEqual(usage["input_tokens"], 500)
        self.assertEqual(usage["output_tokens"], 200)

    def test_nested_model_object_recognized(self) -> None:
        payload = {
            "status": "implemented",
            "change": "ex",
            "model": {
                "provider": "openai",
                "model_id": "gpt-4",
            },
        }
        _, model = telemetry_mod.extract_usage_and_model(payload, None)
        self.assertEqual(model["provider"], "openai")
        self.assertEqual(model["model_id"], "gpt-4")

    # -- Sidecar tests (tasks 4.1-4.6) ---------------------------------------

    def _write_sidecar(self, *records: dict) -> Path:
        """Write a sidecar JSONL file and return its path."""
        p = self.log_dir / f"sidecar-{id(records)}.jsonl"
        lines = "\n".join(json.dumps(r) for r in records) + "\n"
        p.write_text(lines, encoding="utf-8")
        return p

    def _sidecar_identity(self, plan_name="test-plan", run_id="run-001",
                          change_id="ex", stage="implement", round_num=1):
        return {
            "plan_name": plan_name,
            "run_id": run_id,
            "change_id": change_id,
            "stage": stage,
            "round": round_num,
        }

    # 4.1 -- Valid final sidecar populates usage, model, usage_source
    def test_valid_final_sidecar_populates_usage_and_model(self) -> None:
        sidecar_path = self._write_sidecar(
            {
                "schema_version": 1,
                "event_type": "final",
                "emitted_at": "2026-07-01T10:00:05Z",
                "usage": {
                    "input_tokens": 500,
                    "output_tokens": 200,
                    "total_tokens": 700,
                },
                "model": {
                    "provider": "openai",
                    "model_id": "gpt-4o",
                },
                **self._sidecar_identity(),
            },
        )
        # Worker JSON has no usage or model
        payload = {"status": "implemented", "change": "ex"}
        usage, model = telemetry_mod.extract_usage_and_model(
            payload, None,
            sidecar_path=sidecar_path,
            plan_name="test-plan", run_id="run-001", change_id="ex",
            stage="implement", round_num=1,
            is_normal_completion=True,
        )
        self.assertTrue(usage["usage_available"])
        self.assertEqual(usage["usage_source"], "opencode_plugin")
        self.assertEqual(usage["input_tokens"], 500)
        self.assertEqual(usage["output_tokens"], 200)
        self.assertEqual(usage["total_tokens"], 700)
        self.assertEqual(model["provider"], "openai")
        self.assertEqual(model["model_id"], "gpt-4o")

    # 4.2 -- Missing sidecar preserves unavailable usage
    def test_missing_sidecar_preserves_unavailable_usage(self) -> None:
        missing_path = self.log_dir / "nonexistent.jsonl"
        payload = {"status": "implemented", "change": "ex"}
        usage, model = telemetry_mod.extract_usage_and_model(
            payload, None,
            sidecar_path=missing_path,
            plan_name="test-plan", run_id="run-001", change_id="ex",
            stage="implement", round_num=1,
            is_normal_completion=True,
        )
        self._assert_usage_unavailable(usage)
        self._assert_model_null(model)

    # 4.3 -- Malformed JSONL, invalid values, unsupported schemas,
    #        unknown event types, identity mismatches ignored
    def test_sidecar_invalid_records_ignored_independently(self) -> None:
        # Write sidecar manually so we can include a genuinely malformed
        # JSONL line (not just valid-JSON records with bad content).
        sidecar_path = self.log_dir / "malformed-sidecar.jsonl"
        identity = self._sidecar_identity()
        records: list[str] = [
            # Truly malformed JSON — not a parseable JSON value at all
            "{broken json that cannot be parsed",
            # Garbage text that does not even look like JSON
            "just some garbage text without braces",
            # Valid JSON but an array (not a dict) — a subclass of malformed record
            '[1, 2, 3]',
            # Unsupported schema
            json.dumps({
                "schema_version": 99,
                "event_type": "final",
                "emitted_at": "2026-07-01T10:00:00Z",
                "usage": {"input_tokens": 999},
                **identity,
            }),
            # Unknown event type
            json.dumps({
                "schema_version": 1,
                "event_type": "unknown-type",
                "emitted_at": "2026-07-01T10:00:01Z",
                "usage": {"input_tokens": 888},
                **identity,
            }),
            # Identity mismatch
            json.dumps({
                "schema_version": 1,
                "event_type": "final",
                "emitted_at": "2026-07-01T10:00:02Z",
                "usage": {"input_tokens": 777},
                **self._sidecar_identity(change_id="wrong-change"),
            }),
            # Negative token value — ignored
            json.dumps({
                "schema_version": 1,
                "event_type": "final",
                "emitted_at": "2026-07-01T10:00:03Z",
                "usage": {"input_tokens": -5, "output_tokens": 20},
                **identity,
            }),
            # Valid record — should be selected
            json.dumps({
                "schema_version": 1,
                "event_type": "final",
                "emitted_at": "2026-07-01T10:00:04Z",
                "usage": {"input_tokens": 300, "output_tokens": 100},
                "model": {"provider": "anthropic", "model_id": "claude-4"},
                **identity,
            }),
        ]
        sidecar_path.write_text("\n".join(records) + "\n", encoding="utf-8")

        payload = {"status": "implemented", "change": "ex"}
        usage, model = telemetry_mod.extract_usage_and_model(
            payload, None,
            sidecar_path=sidecar_path,
            plan_name="test-plan", run_id="run-001", change_id="ex",
            stage="implement", round_num=1,
            is_normal_completion=True,
        )
        # The valid record at the end should have been selected
        self.assertTrue(usage["usage_available"])
        self.assertEqual(usage["usage_source"], "opencode_plugin")
        self.assertEqual(usage["input_tokens"], 300)
        self.assertEqual(usage["output_tokens"], 100)
        self.assertEqual(model["provider"], "anthropic")
        self.assertEqual(model["model_id"], "claude-4")

    # 4.4 -- Timeout uses latest valid incremental record when no final
    def test_timeout_uses_incremental_when_no_final(self) -> None:
        sidecar_path = self._write_sidecar(
            {
                "schema_version": 1,
                "event_type": "incremental",
                "emitted_at": "2026-07-01T10:00:01Z",
                "usage": {"input_tokens": 100, "output_tokens": 50},
                **self._sidecar_identity(),
            },
            {
                "schema_version": 1,
                "event_type": "incremental",
                "emitted_at": "2026-07-01T10:00:03Z",  # later
                "usage": {"input_tokens": 400, "output_tokens": 150},
                **self._sidecar_identity(),
            },
        )
        payload = {"status": "implemented", "change": "ex"}
        usage, model = telemetry_mod.extract_usage_and_model(
            payload, None,
            sidecar_path=sidecar_path,
            plan_name="test-plan", run_id="run-001", change_id="ex",
            stage="implement", round_num=1,
            is_normal_completion=False,  # timeout = non-normal
        )
        self.assertTrue(usage["usage_available"])
        self.assertEqual(usage["usage_source"], "opencode_plugin")
        # Latest incremental record wins
        self.assertEqual(usage["input_tokens"], 400)
        self.assertEqual(usage["output_tokens"], 150)

    # 4.5 -- Completed stage with only incremental records ignores them
    def test_completed_stage_ignores_incremental_only_sidecar(self) -> None:
        sidecar_path = self._write_sidecar(
            {
                "schema_version": 1,
                "event_type": "incremental",
                "emitted_at": "2026-07-01T10:00:01Z",
                "usage": {"input_tokens": 200, "output_tokens": 80},
                **self._sidecar_identity(),
            },
        )
        payload = {"status": "implemented", "change": "ex"}
        usage, model = telemetry_mod.extract_usage_and_model(
            payload, None,
            sidecar_path=sidecar_path,
            plan_name="test-plan", run_id="run-001", change_id="ex",
            stage="implement", round_num=1,
            is_normal_completion=True,  # normal completion
        )
        # Normal completion with only incremental => sidecar ignored
        self._assert_usage_unavailable(usage)
        self._assert_model_null(model)

    # 4.6 -- Worker JSON and log metadata keep precedence over sidecar
    def test_worker_json_precedence_over_sidecar(self) -> None:
        sidecar_path = self._write_sidecar(
            {
                "schema_version": 1,
                "event_type": "final",
                "emitted_at": "2026-07-01T10:00:05Z",
                "usage": {"input_tokens": 999, "output_tokens": 888},
                "model": {"provider": "sidecar-provider", "model_id": "sidecar-model"},
                **self._sidecar_identity(),
            },
        )
        # Worker JSON has usage and model — both take precedence
        payload = {
            "status": "implemented",
            "change": "ex",
            "usage": {"input_tokens": 100, "output_tokens": 50},
            "model": {"provider": "worker-provider", "model_id": "worker-model"},
        }
        usage, model = telemetry_mod.extract_usage_and_model(
            payload, None,
            sidecar_path=sidecar_path,
            plan_name="test-plan", run_id="run-001", change_id="ex",
            stage="implement", round_num=1,
            is_normal_completion=True,
        )
        self.assertTrue(usage["usage_available"])
        self.assertEqual(usage["usage_source"], "worker_json")
        self.assertEqual(usage["input_tokens"], 100)
        self.assertEqual(usage["output_tokens"], 50)
        self.assertEqual(model["provider"], "worker-provider")
        self.assertEqual(model["model_id"], "worker-model")

    def test_log_metadata_precedence_over_sidecar(self) -> None:
        sidecar_path = self._write_sidecar(
            {
                "schema_version": 1,
                "event_type": "final",
                "emitted_at": "2026-07-01T10:00:05Z",
                "usage": {"input_tokens": 999, "output_tokens": 888},
                "model": {"provider": "sidecar-provider", "model_id": "sidecar-model"},
                **self._sidecar_identity(),
            },
        )
        log_path = self._write_log(
            '{"input_tokens": 500, "output_tokens": 250}',
            '{"provider": "log-provider", "model_id": "log-model"}',
        )
        # Worker JSON has no usage or model — log should win over sidecar
        payload = {"status": "implemented", "change": "ex"}
        usage, model = telemetry_mod.extract_usage_and_model(
            payload, log_path,
            sidecar_path=sidecar_path,
            plan_name="test-plan", run_id="run-001", change_id="ex",
            stage="implement", round_num=1,
            is_normal_completion=True,
        )
        self.assertTrue(usage["usage_available"])
        self.assertEqual(usage["usage_source"], "log_metadata")
        self.assertEqual(usage["input_tokens"], 500)
        self.assertEqual(usage["output_tokens"], 250)
        self.assertEqual(model["provider"], "log-provider")
        self.assertEqual(model["model_id"], "log-model")

    def test_sidecar_model_fallback_when_worker_has_usage_but_no_model(self) -> None:
        """Sidecar model metadata fills telemetry even when worker/log usage
        exists but model metadata does not (the key fix for this round)."""
        sidecar_path = self._write_sidecar(
            {
                "schema_version": 1,
                "event_type": "final",
                "emitted_at": "2026-07-01T10:00:05Z",
                # No token usage in sidecar — model-only record
                "model": {"provider": "openai", "model_id": "gpt-4o"},
                **self._sidecar_identity(),
            },
        )
        # Worker JSON has usage but no model
        payload = {
            "status": "implemented",
            "change": "ex",
            "usage": {"input_tokens": 500, "output_tokens": 200},
        }
        usage, model = telemetry_mod.extract_usage_and_model(
            payload, None,
            sidecar_path=sidecar_path,
            plan_name="test-plan", run_id="run-001", change_id="ex",
            stage="implement", round_num=1,
            is_normal_completion=True,
        )
        # Usage comes from worker
        self.assertTrue(usage["usage_available"])
        self.assertEqual(usage["usage_source"], "worker_json")
        self.assertEqual(usage["input_tokens"], 500)
        self.assertEqual(usage["output_tokens"], 200)
        # Model comes from sidecar because worker had none
        self.assertEqual(model["provider"], "openai")
        self.assertEqual(model["model_id"], "gpt-4o")

    def test_sidecar_model_fallback_when_log_has_usage_but_no_model(self) -> None:
        """Sidecar model metadata fills when log provides usage but not model."""
        sidecar_path = self._write_sidecar(
            {
                "schema_version": 1,
                "event_type": "final",
                "emitted_at": "2026-07-01T10:00:05Z",
                "model": {"provider": "anthropic", "model_id": "claude-4"},
                **self._sidecar_identity(),
            },
        )
        # Log has usage tokens but no model fields
        log_path = self._write_log(
            '{"input_tokens": 300, "output_tokens": 100}',
        )
        payload = {"status": "implemented", "change": "ex"}
        usage, model = telemetry_mod.extract_usage_and_model(
            payload, log_path,
            sidecar_path=sidecar_path,
            plan_name="test-plan", run_id="run-001", change_id="ex",
            stage="implement", round_num=1,
            is_normal_completion=True,
        )
        # Usage comes from log
        self.assertTrue(usage["usage_available"])
        self.assertEqual(usage["usage_source"], "log_metadata")
        self.assertEqual(usage["input_tokens"], 300)
        self.assertEqual(usage["output_tokens"], 100)
        # Model comes from sidecar because log had none
        self.assertEqual(model["provider"], "anthropic")
        self.assertEqual(model["model_id"], "claude-4")

    # -- Regression: model-only sidecar with no higher-precedence usage ----

    def test_model_only_sidecar_no_higher_precedence_does_not_mark_usage_available(self) -> None:
        """Model-only sidecar records only backfill model identity and
        must never set usage_available, usage_source, or estimated cost
        when no token counts are present and no higher-precedence source
        provides usage."""
        sidecar_path = self._write_sidecar(
            {
                "schema_version": 1,
                "event_type": "final",
                "emitted_at": "2026-07-01T10:00:05Z",
                # No usage / token fields at all — model-only record
                "model": {"provider": "openai", "model_id": "gpt-4o"},
                **self._sidecar_identity(),
            },
        )
        # Worker JSON has no usage and no model — sidecar is the only source
        payload = {"status": "implemented", "change": "ex"}
        usage, model = telemetry_mod.extract_usage_and_model(
            payload, None,
            sidecar_path=sidecar_path,
            plan_name="test-plan", run_id="run-001", change_id="ex",
            stage="implement", round_num=1,
            is_normal_completion=True,
        )
        # Model identity comes from sidecar — it was missing from worker
        self.assertEqual(model["provider"], "openai")
        self.assertEqual(model["model_id"], "gpt-4o")
        # Usage must NOT be marked available — no token counts exist
        self._assert_usage_unavailable(usage)


class TelemetryModelAttributionTests(unittest.TestCase):
    """model.attribution distinguishes runtime-observed identity from the
    configured dsh fallback, and stays null when no identity is available."""

    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.log_dir = Path(self.tmp.name)

    def tearDown(self) -> None:
        self.tmp.cleanup()

    def _write_log(self, *lines: str) -> Path:
        p = self.log_dir / f"attrib-{hash(lines)}.log"
        p.write_text("\n".join(lines) + "\n", encoding="utf-8")
        return p

    def test_worker_json_observed_attribution(self) -> None:
        payload = {"status": "implemented", "change": "ex",
                   "model": {"provider": "openai", "model_id": "gpt-5.5"}}
        _, model = telemetry_mod.extract_usage_and_model(payload, None)
        self.assertEqual(model["attribution"], "observed")

    def test_log_metadata_observed_attribution(self) -> None:
        log_path = self._write_log('{"provider": "openai", "model_id": "gpt-5.5"}')
        _, model = telemetry_mod.extract_usage_and_model(
            {"status": "implemented", "change": "ex"}, log_path
        )
        self.assertEqual(model["attribution"], "observed")

    def test_sidecar_observed_attribution(self) -> None:
        sidecar_path = self._write_sidecar_attrib(
            {"provider": "openai", "model_id": "gpt-4o"}
        )
        _, model = telemetry_mod.extract_usage_and_model(
            {"status": "implemented", "change": "ex"}, None,
            sidecar_path=sidecar_path,
            plan_name="test-plan", run_id="run-001", change_id="ex",
            stage="implement", round_num=1, is_normal_completion=True,
        )
        self.assertEqual(model["attribution"], "observed")

    def test_unavailable_identity_has_null_attribution(self) -> None:
        _, model = telemetry_mod.extract_usage_and_model(
            {"status": "implemented", "change": "ex"}, None
        )
        self.assertIsNone(model["provider"])
        self.assertIsNone(model["model_id"])
        self.assertIsNone(model["attribution"])

    def test_worker_observed_beats_configured_fallback(self) -> None:
        """Observed identity from any source is never relabeled configured
        by a later fallback in _record_stage_telemetry."""
        payload = {
            "status": "implemented", "change": "ex",
            "model": {"provider": "openai", "model_id": "gpt-5.5"},
        }
        _, model = telemetry_mod.extract_usage_and_model(payload, None)
        self.assertEqual(model["attribution"], "observed")

    def _write_sidecar_attrib(self, model: dict) -> Path:
        sidecar = self.log_dir / f"attrib-sidecar-{hash(str(model))}.jsonl"
        sidecar.write_text(
            json.dumps(
                {
                    "schema_version": 1,
                    "event_type": "final",
                    "emitted_at": "2026-07-01T10:00:05Z",
                    "model": model,
                    "plan_name": "test-plan", "run_id": "run-001",
                    "change_id": "ex", "stage": "implement", "round": 1,
                }
            )
            + "\n",
            encoding="utf-8",
        )
        return sidecar


class DshTelemetryModelAttributionTests(unittest.TestCase):
    """dsh stage invokes carry no ``--model``/``--agent`` flag, so telemetry
    must attribute the resolved role model from ``cfg["models"]``."""

    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.repo = Path(self.tmp.name)
        self.repo.mkdir(parents=True, exist_ok=True)
        self.cid = "add-dsh-telemetry"
        self.plan_name = "dsh-telemetry-plan"

    def tearDown(self) -> None:
        self.tmp.cleanup()

    def _cfg(self) -> dict:
        models = {
            "implementer": ResolvedModel(
                role="implementer", model="deepseek/deepseek-chat", source="test"
            ),
            "reviewer": ResolvedModel(
                role="reviewer", model="deepseek/deepseek-reasoner", source="test"
            ),
            "archiver": ResolvedModel(
                role="archiver", model="deepseek/deepseek-v4", source="test"
            ),
        }
        return {
            "name": self.plan_name,
            "adapter": "dsh",
            "implement_invoke": "opsx-dsh-worker --role implementer",
            "review_invoke": "opsx-dsh-worker --role reviewer",
            "archive_invoke": "opsx-dsh-worker --role archiver",
            "models": models,
            "changes": {
                self.cid: {"timeout_minutes": 1},
            },
        }

    def test_resolved_role_model_parses_dsh_role_model(self) -> None:
        model = telemetry_mod._resolved_role_model(self._cfg(), "implement")
        self.assertEqual(model["provider"], "deepseek")
        self.assertEqual(model["model_id"], "deepseek-chat")
        self.assertIsNone(model["model_alias"])

    def test_resolved_role_model_ignores_non_dsh_adapter(self) -> None:
        cfg = self._cfg()
        cfg["adapter"] = "opencode"
        model = telemetry_mod._resolved_role_model(cfg, "implement")
        self.assertIsNone(model["provider"])
        self.assertIsNone(model["model_id"])

    def test_record_stage_telemetry_attributes_dsh_role_model(self) -> None:
        cfg = self._cfg()
        state = {"plan": self.plan_name, "approvals": [], "changes": {}}
        payload = {"status": "implemented", "change": self.cid}
        log_path = self.repo / ".opsx-plan" / "logs" / f"{self.cid}.implement.r1.1.log"
        log_path.parent.mkdir(parents=True, exist_ok=True)
        log_path.write_text('{"status": "implemented"}\n', encoding="utf-8")

        telemetry_mod._record_stage_telemetry(
            self.repo, cfg, state, self.cid, "implement", 1,
            "2026-08-21T10:00:00", "2026-08-21T10:00:01", 1000,
            "completed", None, payload, log_path,
        )

        jsonl = (
            self.repo / ".opsx-plan" / "telemetry" / f"{self.plan_name}.jsonl"
        )
        self.assertTrue(jsonl.is_file(), f"expected telemetry at {jsonl}")
        records = [
            json.loads(line)
            for line in jsonl.read_text(encoding="utf-8").splitlines()
            if line.strip()
        ]
        self.assertEqual(len(records), 1)
        self.assertEqual(records[0]["model"]["provider"], "deepseek")
        self.assertEqual(records[0]["model"]["model_id"], "deepseek-chat")
        self.assertEqual(records[0]["model"]["attribution"], "configured")

    def test_record_stage_telemetry_unresolved_dsh_model_stays_null(self) -> None:
        cfg = self._cfg()
        cfg["models"] = {}
        state = {"plan": self.plan_name, "approvals": [], "changes": {}}
        payload = {"status": "implemented", "change": self.cid}
        log_path = self.repo / ".opsx-plan" / "logs" / f"{self.cid}.implement.r1.1.log"
        log_path.parent.mkdir(parents=True, exist_ok=True)
        log_path.write_text('{"status": "implemented"}\n', encoding="utf-8")

        telemetry_mod._record_stage_telemetry(
            self.repo, cfg, state, self.cid, "implement", 1,
            "2026-08-21T10:00:00", "2026-08-21T10:00:01", 1000,
            "completed", None, payload, log_path,
        )

        jsonl = (
            self.repo / ".opsx-plan" / "telemetry" / f"{self.plan_name}.jsonl"
        )
        records = [
            json.loads(line)
            for line in jsonl.read_text(encoding="utf-8").splitlines()
            if line.strip()
        ]
        self.assertEqual(len(records), 1)
        self.assertIsNone(records[0]["model"]["provider"])
        self.assertIsNone(records[0]["model"]["model_id"])
        self.assertIsNone(records[0]["model"]["attribution"])

    def test_record_stage_telemetry_observed_when_worker_output_has_model(self) -> None:
        """A dsh stage whose worker output reveals model identity is
        attributed observed, never configured."""
        cfg = self._cfg()
        state = {"plan": self.plan_name, "approvals": [], "changes": {}}
        payload = {
            "status": "implemented", "change": self.cid,
            "model": {"provider": "deepseek", "model_id": "deepseek-chat"},
        }
        log_path = self.repo / ".opsx-plan" / "logs" / f"{self.cid}.implement.r1.1.log"
        log_path.parent.mkdir(parents=True, exist_ok=True)
        log_path.write_text('{"status": "implemented"}\n', encoding="utf-8")

        telemetry_mod._record_stage_telemetry(
            self.repo, cfg, state, self.cid, "implement", 1,
            "2026-08-21T10:00:00", "2026-08-21T10:00:01", 1000,
            "completed", None, payload, log_path,
        )

        jsonl = (
            self.repo / ".opsx-plan" / "telemetry" / f"{self.plan_name}.jsonl"
        )
        records = [
            json.loads(line)
            for line in jsonl.read_text(encoding="utf-8").splitlines()
            if line.strip()
        ]
        self.assertEqual(records[0]["model"]["provider"], "deepseek")
        self.assertEqual(records[0]["model"]["model_id"], "deepseek-chat")
        self.assertEqual(records[0]["model"]["attribution"], "observed")

    def test_record_stage_telemetry_invocation_model_is_observed(self) -> None:
        """An explicit --model in the worker invocation is attribution
        observed; only the resolved dsh role model is configured."""
        cfg = self._cfg()
        cfg["adapter"] = "opencode"
        cfg["implement_invoke"] = 'opencode run --model "acme/acme-model"'
        state = {"plan": self.plan_name, "approvals": [], "changes": {}}
        payload = {"status": "implemented", "change": self.cid}
        log_path = self.repo / ".opsx-plan" / "logs" / f"{self.cid}.implement.r1.1.log"
        log_path.parent.mkdir(parents=True, exist_ok=True)
        log_path.write_text('{"status": "implemented"}\n', encoding="utf-8")

        telemetry_mod._record_stage_telemetry(
            self.repo, cfg, state, self.cid, "implement", 1,
            "2026-08-21T10:00:00", "2026-08-21T10:00:01", 1000,
            "completed", None, payload, log_path,
        )

        jsonl = (
            self.repo / ".opsx-plan" / "telemetry" / f"{self.plan_name}.jsonl"
        )
        records = [
            json.loads(line)
            for line in jsonl.read_text(encoding="utf-8").splitlines()
            if line.strip()
        ]
        self.assertEqual(records[0]["model"]["provider"], "acme")
        self.assertEqual(records[0]["model"]["model_id"], "acme-model")
        self.assertEqual(records[0]["model"]["attribution"], "observed")


class ClaudeResultEnvelopeUsagePrecedenceTests(unittest.TestCase):
    """Tests for the claude_result_json usage/model source and its precedence."""

    def setUp(self) -> None:
        self.opsx_plan = load_opsx_plan()
        self.tmp = tempfile.TemporaryDirectory()
        self.log_dir = Path(self.tmp.name)

    def tearDown(self) -> None:
        self.tmp.cleanup()

    def _write_log(self, *lines: str) -> Path:
        p = self.log_dir / f"test-{hash(lines)}.log"
        p.write_text("\n".join(lines) + "\n", encoding="utf-8")
        return p

    def test_envelope_is_only_usage_source(self) -> None:
        payload = {"status": "implemented", "change": "ex"}
        envelope = {
            "type": "result",
            "result": "...",
            "usage": {"input_tokens": 30, "output_tokens": 12},
        }
        usage, model = telemetry_mod.extract_usage_and_model(
            payload, None, envelope=envelope,
        )
        self.assertTrue(usage["usage_available"])
        self.assertEqual(usage["usage_source"], "claude_result_json")
        self.assertEqual(usage["input_tokens"], 30)
        self.assertEqual(usage["output_tokens"], 12)

    def test_worker_json_wins_over_envelope(self) -> None:
        payload = {
            "status": "implemented",
            "change": "ex",
            "usage": {"input_tokens": 100, "output_tokens": 50},
        }
        envelope = {
            "type": "result",
            "result": "...",
            "usage": {"input_tokens": 999, "output_tokens": 888},
        }
        usage, _ = telemetry_mod.extract_usage_and_model(
            payload, None, envelope=envelope,
        )
        self.assertEqual(usage["usage_source"], "worker_json")
        self.assertEqual(usage["input_tokens"], 100)
        self.assertEqual(usage["output_tokens"], 50)

    def test_envelope_outranks_log_metadata(self) -> None:
        log_path = self._write_log(
            '{"input_tokens": 777, "output_tokens": 666}',
        )
        payload = {"status": "implemented", "change": "ex"}
        envelope = {
            "type": "result",
            "result": "...",
            "usage": {"input_tokens": 40, "output_tokens": 15},
        }
        usage, _ = telemetry_mod.extract_usage_and_model(
            payload, log_path, envelope=envelope,
        )
        self.assertEqual(usage["usage_source"], "claude_result_json")
        self.assertEqual(usage["input_tokens"], 40)
        self.assertEqual(usage["output_tokens"], 15)

    def test_log_metadata_wins_when_no_envelope(self) -> None:
        log_path = self._write_log(
            '{"input_tokens": 777, "output_tokens": 666}',
        )
        payload = {"status": "implemented", "change": "ex"}
        usage, _ = telemetry_mod.extract_usage_and_model(
            payload, log_path, envelope=None,
        )
        self.assertEqual(usage["usage_source"], "log_metadata")
        self.assertEqual(usage["input_tokens"], 777)

    def test_envelope_model_identity_recorded_when_worker_json_has_none(self) -> None:
        payload = {"status": "implemented", "change": "ex"}
        envelope = {
            "type": "result",
            "result": "...",
            "model": {"provider": "anthropic", "model_id": "claude-opus-5"},
        }
        _, model = telemetry_mod.extract_usage_and_model(
            payload, None, envelope=envelope,
        )
        self.assertEqual(model["provider"], "anthropic")
        self.assertEqual(model["model_id"], "claude-opus-5")

    def test_worker_json_model_wins_over_envelope_model(self) -> None:
        payload = {
            "status": "implemented",
            "change": "ex",
            "model": {"provider": "worker-provider", "model_id": "worker-model"},
        }
        envelope = {
            "type": "result",
            "result": "...",
            "model": {"provider": "anthropic", "model_id": "claude-opus-5"},
        }
        _, model = telemetry_mod.extract_usage_and_model(
            payload, None, envelope=envelope,
        )
        self.assertEqual(model["provider"], "worker-provider")
        self.assertEqual(model["model_id"], "worker-model")

    def test_streamed_intermediate_usage_does_not_displace_envelope_totals(self) -> None:
        """The orchestrator passes the *selected* (final) envelope only, so an
        intermediate streamed message's partial usage never reaches here."""
        payload = {"status": "implemented", "change": "ex"}
        final_envelope = {
            "type": "result",
            "result": "...",
            "usage": {"input_tokens": 50, "output_tokens": 25},
        }
        usage, _ = telemetry_mod.extract_usage_and_model(
            payload, None, envelope=final_envelope,
        )
        self.assertEqual(usage["input_tokens"], 50)
        self.assertEqual(usage["output_tokens"], 25)

    def test_cache_creation_input_tokens_populates_cached_input(self) -> None:
        payload = {"status": "implemented", "change": "ex"}
        envelope = {
            "type": "result",
            "result": "...",
            "usage": {
                "input_tokens": 10,
                "output_tokens": 5,
                "cache_creation_input_tokens": 7,
            },
        }
        usage, _ = telemetry_mod.extract_usage_and_model(
            payload, None, envelope=envelope,
        )
        self.assertEqual(usage["cached_input_tokens"], 7)

    def test_real_envelope_model_usage_shape_resolves_model_identity(self) -> None:
        """Regression test against the real Claude Code CLI envelope shape,
        which carries model identity under `modelUsage` (keyed by canonical
        model id), not a flat/nested `model` field."""
        payload = {"status": "implemented", "change": "ex"}
        envelope = {
            "type": "result",
            "result": "...",
            "usage": {"input_tokens": 34, "output_tokens": 2515},
            "modelUsage": {
                "claude-haiku-4-5-20251001": {
                    "inputTokens": 687,
                    "outputTokens": 2528,
                    "cacheReadInputTokens": 26178,
                    "cacheCreationInputTokens": 11204,
                    "canonicalModel": "claude-haiku-4-5",
                    "provider": "firstParty",
                }
            },
        }
        _, model = telemetry_mod.extract_usage_and_model(
            payload, None, envelope=envelope,
        )
        self.assertEqual(model["provider"], "anthropic")
        self.assertEqual(model["model_id"], "claude-haiku-4-5")

    def test_real_envelope_model_usage_picks_highest_token_entry(self) -> None:
        payload = {"status": "implemented", "change": "ex"}
        envelope = {
            "type": "result",
            "result": "...",
            "modelUsage": {
                "claude-haiku-4-5-20251001": {
                    "inputTokens": 10, "outputTokens": 5,
                    "canonicalModel": "claude-haiku-4-5",
                },
                "claude-sonnet-4-5-20250929": {
                    "inputTokens": 500, "outputTokens": 200,
                    "canonicalModel": "claude-sonnet-4-5",
                },
            },
        }
        _, model = telemetry_mod.extract_usage_and_model(
            payload, None, envelope=envelope,
        )
        self.assertEqual(model["model_id"], "claude-sonnet-4-5")

    def test_generic_model_field_still_wins_over_model_usage(self) -> None:
        payload = {"status": "implemented", "change": "ex"}
        envelope = {
            "type": "result",
            "result": "...",
            "model": {"provider": "anthropic", "model_id": "claude-opus-5"},
            "modelUsage": {
                "claude-haiku-4-5-20251001": {
                    "inputTokens": 999, "canonicalModel": "claude-haiku-4-5",
                }
            },
        }
        _, model = telemetry_mod.extract_usage_and_model(
            payload, None, envelope=envelope,
        )
        self.assertEqual(model["model_id"], "claude-opus-5")


class DirectStageUsageIntegrationTests(unittest.TestCase):
    """Integration tests that verify usage/model appear in telemetry records
    written by the full run_direct_change pipeline."""

    def setUp(self) -> None:
        self.opsx_plan = load_opsx_plan()
        self.tmp = tempfile.TemporaryDirectory()
        self.repo = Path(self.tmp.name)
        git(self.repo, "init")
        (self.repo / "tracked.txt").write_text("base\n", encoding="utf-8")
        git(self.repo, "add", "tracked.txt")
        git(
            self.repo,
            "-c",
            "user.email=test@example.invalid",
            "-c",
            "user.name=Test User",
            "commit",
            "-m",
            "init",
        )
        self.cid = "add-usage-integration"
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
        self._saved_checks = groundtruth_mod.run_fast_checks

    def tearDown(self) -> None:
        self.opsx_plan.invoke_direct_stage = self._saved_invoke
        groundtruth_mod.run_fast_checks = self._saved_checks
        self.tmp.cleanup()

    def write_authored_change(self, cid: str) -> None:
        cdir = self.repo / "openspec" / "changes" / cid
        cdir.mkdir(parents=True)
        (cdir / "proposal.md").write_text("## Why\n", encoding="utf-8")
        (cdir / "tasks.md").write_text(
            "## 1. Tasks\n\n- [ ] 1.1 Example task\n- [ ] 1.2 Example task\n",
            encoding="utf-8",
        )

    def _read_telemetry(self) -> list[dict]:
        jsonl = self.repo / ".opsx-plan" / "telemetry" / f"{self.plan_name}.jsonl"
        if not jsonl.is_file():
            return []
        records: list[dict] = []
        for line in jsonl.read_text(encoding="utf-8").splitlines():
            if line.strip():
                records.append(json.loads(line))
        return records

    # -- 4.1 integration: full usage in worker JSON -> telemetry record ----

    def test_implement_with_full_usage_payload_produces_populated_record(self) -> None:
        self.write_authored_change(self.cid)
        record = state_mod.rec(self.state, self.cid)
        record["max_rounds"] = self.cfg["max_rounds"]
        record["tracked_change_files"] = state_mod.change_context_paths(
            self.repo, self.cid
        )

        def fake_invoke(repo, cfg, cid, stage, round_num, input_block):
            log_path = self.opsx_plan.next_stage_log_path(repo, cid, stage, round_num)
            log_path.parent.mkdir(parents=True, exist_ok=True)
            result = {
                "status": "implemented",
                "change": self.cid,
                "round": 1,
                "progress_made": True,
                "completed_tasks": ["1.1", "1.2"],
                "remaining_tasks": [],
                "task_counts": {"complete": 2, "total": 2},
                "files_touched": [],
                "known_change_files": [],
                "summary": "done",
                "usage": {
                    "input_tokens": 1500,
                    "output_tokens": 300,
                    "cached_input_tokens": 200,
                    "reasoning_tokens": 100,
                    "total_tokens": 2100,
                },
            }
            log_path.write_text(json.dumps(result) + "\n", encoding="utf-8")
            return "exited", log_path

        self.opsx_plan.invoke_direct_stage = fake_invoke
        # Second stage (review) must also execute; use timeout to stop.
        self.opsx_plan.run_direct_change(self.repo, self.cfg, self.state, self.cid)

        records = self._read_telemetry()
        impl = [r for r in records if r["stage"] == "implement"]
        self.assertGreaterEqual(len(impl), 1)
        u = impl[0]["usage"]
        self.assertTrue(u["usage_available"])
        self.assertEqual(u["usage_source"], "worker_json")
        self.assertEqual(u["input_tokens"], 1500)
        self.assertEqual(u["output_tokens"], 300)
        self.assertEqual(u["cached_input_tokens"], 200)
        self.assertEqual(u["reasoning_tokens"], 100)
        self.assertEqual(u["total_tokens"], 2100)

    # -- 4.5 integration: log metadata fallback in telemetry ----------------

    def test_implement_with_log_usage_produces_populated_record(self) -> None:
        self.write_authored_change(self.cid)
        record = state_mod.rec(self.state, self.cid)
        record["max_rounds"] = self.cfg["max_rounds"]
        record["tracked_change_files"] = state_mod.change_context_paths(
            self.repo, self.cid
        )

        def fake_invoke(repo, cfg, cid, stage, round_num, input_block):
            log_path = self.opsx_plan.next_stage_log_path(repo, cid, stage, round_num)
            log_path.parent.mkdir(parents=True, exist_ok=True)
            # Worker JSON has no usage, but log contains usage metadata
            log_body = (
                "# worker run\n"
                + '{"input_tokens": 800, "output_tokens": 150}\n'
                + "# more log lines\n"
                + '{"status":"implemented","change":"add-usage-integration","round":1,'
                + '"progress_made":true,"completed_tasks":[],"remaining_tasks":[],'
                + '"task_counts":{"complete":0,"total":2},"files_touched":[],'
                + '"known_change_files":[],"summary":"done"}\n'
            )
            log_path.write_text(log_body, encoding="utf-8")
            return "exited", log_path

        self.opsx_plan.invoke_direct_stage = fake_invoke
        self.opsx_plan.run_direct_change(self.repo, self.cfg, self.state, self.cid)

        records = self._read_telemetry()
        impl = [r for r in records if r["stage"] == "implement"]
        self.assertGreaterEqual(len(impl), 1)
        u = impl[0]["usage"]
        self.assertTrue(u["usage_available"])
        self.assertEqual(u["usage_source"], "log_metadata")
        self.assertEqual(u["input_tokens"], 800)
        self.assertEqual(u["output_tokens"], 150)

    # -- 4.9 integration: timeout preserves default-unavailable -------------

    def test_timeout_keeps_default_unavailable_usage_in_telemetry(self) -> None:
        self.write_authored_change(self.cid)
        record = state_mod.rec(self.state, self.cid)
        record["max_rounds"] = self.cfg["max_rounds"]

        def fake_invoke(repo, cfg, cid, stage, round_num, input_block):
            log_path = self.opsx_plan.next_stage_log_path(repo, cid, stage, round_num)
            log_path.parent.mkdir(parents=True, exist_ok=True)
            log_path.write_text("# timeout\n", encoding="utf-8")
            return "timeout", log_path

        self.opsx_plan.invoke_direct_stage = fake_invoke
        self.opsx_plan.run_direct_change(self.repo, self.cfg, self.state, self.cid)

        records = self._read_telemetry()
        self.assertGreaterEqual(len(records), 1)
        u = records[0]["usage"]
        self.assertFalse(u["usage_available"])
        self.assertIsNone(u["usage_source"])
        self.assertIsNone(u["input_tokens"])
        self.assertIsNone(u["output_tokens"])

    def test_invalid_output_keeps_default_unavailable_usage_in_telemetry(self) -> None:
        self.write_authored_change(self.cid)
        record = state_mod.rec(self.state, self.cid)
        record["max_rounds"] = self.cfg["max_rounds"]

        def fake_invoke(repo, cfg, cid, stage, round_num, input_block):
            log_path = self.opsx_plan.next_stage_log_path(repo, cid, stage, round_num)
            log_path.parent.mkdir(parents=True, exist_ok=True)
            log_path.write_text("not json\nsecond line\n", encoding="utf-8")
            return "exited", log_path

        self.opsx_plan.invoke_direct_stage = fake_invoke
        self.opsx_plan.run_direct_change(self.repo, self.cfg, self.state, self.cid)

        records = self._read_telemetry()
        self.assertGreaterEqual(len(records), 1)
        u = records[0]["usage"]
        self.assertFalse(u["usage_available"])
        self.assertIsNone(u["usage_source"])

    # -- Model metadata integration -----------------------------------------

    def test_implement_with_model_payload_produces_populated_record(self) -> None:
        self.write_authored_change(self.cid)
        record = state_mod.rec(self.state, self.cid)
        record["max_rounds"] = self.cfg["max_rounds"]
        record["tracked_change_files"] = state_mod.change_context_paths(
            self.repo, self.cid
        )

        def fake_invoke(repo, cfg, cid, stage, round_num, input_block):
            log_path = self.opsx_plan.next_stage_log_path(repo, cid, stage, round_num)
            log_path.parent.mkdir(parents=True, exist_ok=True)
            result = {
                "status": "implemented",
                "change": self.cid,
                "round": 1,
                "progress_made": True,
                "completed_tasks": [],
                "remaining_tasks": [],
                "task_counts": {"complete": 0, "total": 2},
                "files_touched": [],
                "known_change_files": [],
                "summary": "done",
                "model": {
                    "provider": "openai",
                    "model_id": "gpt-5.5",
                    "model_alias": "primary",
                },
            }
            log_path.write_text(json.dumps(result) + "\n", encoding="utf-8")
            return "exited", log_path

        self.opsx_plan.invoke_direct_stage = fake_invoke
        self.opsx_plan.run_direct_change(self.repo, self.cfg, self.state, self.cid)

        records = self._read_telemetry()
        impl = [r for r in records if r["stage"] == "implement"]
        self.assertGreaterEqual(len(impl), 1)
        m = impl[0]["model"]
        self.assertEqual(m["provider"], "openai")
        self.assertEqual(m["model_id"], "gpt-5.5")
        self.assertEqual(m["model_alias"], "primary")

    def test_implement_without_model_payload_uses_invoked_agent_model(self) -> None:
        self.write_authored_change(self.cid)
        record = state_mod.rec(self.state, self.cid)
        record["max_rounds"] = self.cfg["max_rounds"]
        record["tracked_change_files"] = state_mod.change_context_paths(
            self.repo, self.cid
        )

        fake_home = self.repo / "fake-home"
        agent_dir = fake_home / ".config" / "opencode" / "agents"
        agent_dir.mkdir(parents=True)
        (agent_dir / "opsx-implementer.md").write_text(
            "---\nmodel: \"deepseek/deepseek-v4-pro\"\n---\n",
            encoding="utf-8",
        )

        def fake_invoke(repo, cfg, cid, stage, round_num, input_block):
            log_path = self.opsx_plan.next_stage_log_path(repo, cid, stage, round_num)
            log_path.parent.mkdir(parents=True, exist_ok=True)
            if stage == "implement":
                result = {
                    "status": "implemented",
                    "change": self.cid,
                    "round": 1,
                    "progress_made": True,
                    "completed_tasks": [],
                    "remaining_tasks": [],
                    "task_counts": {"complete": 0, "total": 2},
                    "files_touched": [],
                    "known_change_files": [],
                    "summary": "done",
                }
                log_path.write_text(
                    "> opsx-implementer · deepseek-v4-pro\n"
                    + json.dumps(result)
                    + "\n",
                    encoding="utf-8",
                )
                return "exited", log_path
            log_path.write_text("# timeout\n", encoding="utf-8")
            return "timeout", log_path

        self.opsx_plan.invoke_direct_stage = fake_invoke
        with mock.patch.dict(os.environ, {"HOME": str(fake_home)}, clear=False):
            self.opsx_plan.run_direct_change(self.repo, self.cfg, self.state, self.cid)

        records = self._read_telemetry()
        impl = [r for r in records if r["stage"] == "implement"]
        self.assertGreaterEqual(len(impl), 1)
        m = impl[0]["model"]
        self.assertEqual(m["provider"], "deepseek")
        self.assertEqual(m["model_id"], "deepseek-v4-pro")
        self.assertIsNone(m["model_alias"])

    # -- Telemetry write resilience -----------------------------------------

    def test_extraction_failure_does_not_block_telemetry_write(self) -> None:
        """Verify that even if extraction raises, the telemetry record is
        still written with default-unavailable usage."""
        self.write_authored_change(self.cid)
        record = state_mod.rec(self.state, self.cid)
        record["max_rounds"] = self.cfg["max_rounds"]
        record["tracked_change_files"] = state_mod.change_context_paths(
            self.repo, self.cid
        )

        def fake_invoke(repo, cfg, cid, stage, round_num, input_block):
            log_path = self.opsx_plan.next_stage_log_path(repo, cid, stage, round_num)
            log_path.parent.mkdir(parents=True, exist_ok=True)
            result = {
                "status": "implemented",
                "change": self.cid,
                "round": 1,
                "progress_made": True,
                "completed_tasks": [],
                "remaining_tasks": [],
                "task_counts": {"complete": 0, "total": 2},
                "files_touched": [],
                "known_change_files": [],
                "summary": "done",
            }
            log_path.write_text(json.dumps(result) + "\n", encoding="utf-8")
            return "exited", log_path

        self.opsx_plan.invoke_direct_stage = fake_invoke

        class ExtractionError(Exception):
            pass

        def bad_extraction(payload, log_path):
            raise ExtractionError("simulated extraction failure")

        with mock.patch.object(
            telemetry_mod, "extract_usage_and_model",
            side_effect=bad_extraction,
        ):
            self.opsx_plan.run_direct_change(self.repo, self.cfg, self.state, self.cid)

        records = self._read_telemetry()
        self.assertGreaterEqual(len(records), 1)
        u = records[0]["usage"]
        # Must have default-unavailable (the try/except caught the error)
        self.assertFalse(u["usage_available"])
        self.assertIsNone(u["usage_source"])

    # -- Sidecar → estimated-cost telemetry integration (task 4.1) ---------

    def test_sidecar_usage_flows_to_telemetry_with_estimated_cost(self):
        """Task 4.1: Prove that a valid final sidecar produces estimated cost
        in the telemetry record when pricing is available."""
        from lib.pricing import PricingCatalog, UnresolvedPrice

        self.write_authored_change(self.cid)
        record = state_mod.rec(self.state, self.cid)
        record["max_rounds"] = self.cfg["max_rounds"]
        record["tracked_change_files"] = state_mod.change_context_paths(
            self.repo, self.cid
        )

        # Set up pricing catalog so cost estimation produces a real estimate
        catalog_toml = textwrap.dedent("""\
            [catalog]
            version = "1.0.0"
            updated = "2026-01-01"

            [[entries]]
            provider = "openai"
            model_id = "gpt-4o"
            display_name = "GPT-4o"
            billing_mode = "per_token"
            currency = "USD"
            input_price_per_mtok = 2.0
            output_price_per_mtok = 8.0
            effective_date = "2025-01-01"
        """)
        tmp_catalog = tempfile.NamedTemporaryFile(
            mode="w", suffix=".toml", delete=False, encoding="utf-8",
        )
        tmp_catalog.write(catalog_toml)
        tmp_catalog.close()
        catalog_path = Path(tmp_catalog.name)
        saved_catalog = self.opsx_plan.cost_mod._cost_catalog
        try:
            self.opsx_plan.cost_mod._cost_catalog = (
                PricingCatalog(catalog_path=catalog_path),
                UnresolvedPrice,
            )

            def fake_invoke(repo, cfg, cid, stage, round_num, input_block):
                log_path = self.opsx_plan.next_stage_log_path(repo, cid, stage, round_num)
                log_path.parent.mkdir(parents=True, exist_ok=True)

                if stage == "implement":
                    # Worker JSON has no usage or model — sidecar must provide both
                    result = {
                        "status": "implemented",
                        "change": cid,
                        "round": round_num,
                        "progress_made": True,
                        "completed_tasks": ["1.1", "1.2"],
                        "remaining_tasks": [],
                        "task_counts": {"complete": 2, "total": 2},
                        "files_touched": [],
                        "known_change_files": [],
                        "summary": "done",
                    }
                    log_path.write_text(json.dumps(result) + "\n", encoding="utf-8")

                    # Write sidecar file at the path prescribed by OPSX_USAGE_PATH
                    sidecar_path_str = os.environ.get("OPSX_USAGE_PATH", "")
                    if sidecar_path_str:
                        sidecar_record = {
                            "schema_version": 1,
                            "event_type": "final",
                            "emitted_at": "2026-07-01T10:00:05Z",
                            "usage": {
                                "input_tokens": 500000,
                                "output_tokens": 100000,
                            },
                            "model": {
                                "provider": "openai",
                                "model_id": "gpt-4o",
                            },
                            "plan_name": os.environ.get("OPSX_PLAN_NAME", ""),
                            "run_id": os.environ.get("OPSX_RUN_ID", ""),
                            "change_id": os.environ.get("OPSX_CHANGE_ID", cid),
                            "stage": os.environ.get("OPSX_STAGE", stage),
                            "round": int(os.environ.get("OPSX_ROUND", str(round_num))),
                        }
                        Path(sidecar_path_str).write_text(
                            json.dumps(sidecar_record) + "\n", encoding="utf-8"
                        )
                    return "exited", log_path

                elif stage == "review":
                    result = {
                        "status": "reviewed",
                        "change": cid,
                        "round": round_num,
                        "verdict": "pass",
                        "finding_counts": {"critical": 0, "warning": 0, "note": 0},
                        "summary": "review passed",
                        "fix_prompt": "",
                        "next_phase": "archive",
                    }
                    log_path.write_text(json.dumps(result) + "\n", encoding="utf-8")
                    return "exited", log_path

                else:
                    # archive — will fail on missing repo archive evidence,
                    # but implement telemetry is already written by this point
                    log_path.write_text(json.dumps({
                        "status": "archived",
                        "change": cid,
                        "archive_path": "",
                        "spec_sync_status": "no-delta",
                        "commit": "",
                        "summary": "archive claimed without repo evidence",
                    }) + "\n", encoding="utf-8")
                    return "exited", log_path

            saved_invoke = self.opsx_plan.invoke_direct_stage
            try:
                self.opsx_plan.invoke_direct_stage = fake_invoke
                self.opsx_plan.run_direct_change(
                    self.repo, self.cfg, self.state, self.cid
                )
            finally:
                self.opsx_plan.invoke_direct_stage = saved_invoke

            records = self._read_telemetry()
            impl = [r for r in records if r["stage"] == "implement"]
            self.assertGreaterEqual(len(impl), 1, "expected at least one implement record")

            # Usage must come from the sidecar (worker JSON had none)
            u = impl[0]["usage"]
            self.assertTrue(u["usage_available"])
            self.assertEqual(u["usage_source"], "opencode_plugin")
            self.assertEqual(u["input_tokens"], 500000)
            self.assertEqual(u["output_tokens"], 100000)

            # Model identity must come from the sidecar
            m = impl[0]["model"]
            self.assertEqual(m["provider"], "openai")
            self.assertEqual(m["model_id"], "gpt-4o")

            # Cost must be estimated (pricing catalog was set up)
            c = impl[0]["cost"]
            self.assertEqual(c["status"], "estimated")
            # 500k input × $2/mtok = $1.00, 100k output × $8/mtok = $0.80
            self.assertEqual(c["estimated_cost"], 1.80)
            self.assertEqual(c["pricing_catalog_version"], "1.0.0")
            self.assertIsNotNone(c["price_snapshot"])
        finally:
            self.opsx_plan.cost_mod._cost_catalog = saved_catalog
            catalog_path.unlink(missing_ok=True)

    # -- Claude Code envelope -> telemetry integration (task 5.5) -----------

    def test_claude_code_envelope_flows_to_telemetry_with_resolved_cost(self):
        """A Claude Code stage's result envelope must resolve real cost, and
        the resulting record must be renderable by report/dashboard without
        any schema change (they read usage/model/cost generically)."""
        from lib.pricing import PricingCatalog, UnresolvedPrice

        self.cfg["adapter"] = "claude-code"
        self.write_authored_change(self.cid)
        record = state_mod.rec(self.state, self.cid)
        record["max_rounds"] = self.cfg["max_rounds"]
        record["tracked_change_files"] = state_mod.change_context_paths(
            self.repo, self.cid
        )

        catalog_toml = textwrap.dedent("""\
            [catalog]
            version = "1.0.0"
            updated = "2026-01-01"

            [[entries]]
            provider = "anthropic"
            model_id = "claude-opus-5"
            display_name = "Claude Opus 5"
            billing_mode = "per_token"
            currency = "USD"
            input_price_per_mtok = 5.0
            output_price_per_mtok = 25.0
            effective_date = "2025-01-01"
        """)
        tmp_catalog = tempfile.NamedTemporaryFile(
            mode="w", suffix=".toml", delete=False, encoding="utf-8",
        )
        tmp_catalog.write(catalog_toml)
        tmp_catalog.close()
        catalog_path = Path(tmp_catalog.name)
        saved_catalog = self.opsx_plan.cost_mod._cost_catalog
        try:
            self.opsx_plan.cost_mod._cost_catalog = (
                PricingCatalog(catalog_path=catalog_path),
                UnresolvedPrice,
            )

            def fake_invoke(repo, cfg, cid, stage, round_num, input_block):
                log_path = self.opsx_plan.next_stage_log_path(repo, cid, stage, round_num)
                log_path.parent.mkdir(parents=True, exist_ok=True)

                if stage == "implement":
                    worker_json = json.dumps({
                        "status": "implemented",
                        "change": cid,
                        "round": round_num,
                        "progress_made": True,
                        "completed_tasks": ["1.1", "1.2"],
                        "remaining_tasks": [],
                        "task_counts": {"complete": 2, "total": 2},
                        "files_touched": [],
                        "known_change_files": [],
                        "summary": "done",
                    })
                    envelope = {
                        "type": "result",
                        "result": f"Implemented the change.\n{worker_json}",
                        "usage": {
                            "input_tokens": 200000,
                            "output_tokens": 40000,
                        },
                        "model": {
                            "provider": "anthropic",
                            "model_id": "claude-opus-5",
                        },
                    }
                    log_path.write_text(json.dumps(envelope) + "\n", encoding="utf-8")
                    return "exited", log_path

                elif stage == "review":
                    result = {
                        "status": "reviewed",
                        "change": cid,
                        "round": round_num,
                        "verdict": "pass",
                        "finding_counts": {"critical": 0, "warning": 0, "note": 0},
                        "summary": "review passed",
                        "fix_prompt": "",
                        "next_phase": "archive",
                    }
                    log_path.write_text(json.dumps(result) + "\n", encoding="utf-8")
                    return "exited", log_path

                else:
                    log_path.write_text(json.dumps({
                        "status": "archived",
                        "change": cid,
                        "archive_path": "",
                        "spec_sync_status": "no-delta",
                        "commit": "",
                        "summary": "archive claimed without repo evidence",
                    }) + "\n", encoding="utf-8")
                    return "exited", log_path

            saved_invoke = self.opsx_plan.invoke_direct_stage
            try:
                self.opsx_plan.invoke_direct_stage = fake_invoke
                self.opsx_plan.run_direct_change(
                    self.repo, self.cfg, self.state, self.cid
                )
            finally:
                self.opsx_plan.invoke_direct_stage = saved_invoke

            records = self._read_telemetry()
            impl = [r for r in records if r["stage"] == "implement"]
            self.assertGreaterEqual(len(impl), 1, "expected at least one implement record")

            u = impl[0]["usage"]
            self.assertTrue(u["usage_available"])
            self.assertEqual(u["usage_source"], "claude_result_json")
            self.assertEqual(u["input_tokens"], 200000)
            self.assertEqual(u["output_tokens"], 40000)

            m = impl[0]["model"]
            self.assertEqual(m["provider"], "anthropic")
            self.assertEqual(m["model_id"], "claude-opus-5")

            c = impl[0]["cost"]
            self.assertEqual(c["status"], "estimated")
            # 200k input x $5/mtok = $1.00, 40k output x $25/mtok = $1.00
            self.assertEqual(c["estimated_cost"], 2.00)

            # report/dashboard consume telemetry generically (no adapter- or
            # usage_source-specific branching), so aggregation must not choke
            # on this record.
            from lib.metrics.aggregator import aggregate
            result = aggregate(self.repo, self.plan_name, None)
            self.assertEqual(result.plan_metrics.plan_name, self.plan_name)
        finally:
            self.opsx_plan.cost_mod._cost_catalog = saved_catalog
            catalog_path.unlink(missing_ok=True)

