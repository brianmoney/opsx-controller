from __future__ import annotations

import argparse
import importlib.util
import io
import json
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest import mock


SCRIPT = Path(__file__).resolve().parents[2] / "orchestrator" / "opsx-plan.py"


def load_opsx_plan():
    spec = importlib.util.spec_from_file_location("opsx_plan", SCRIPT)
    assert spec is not None
    assert spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def git(repo: Path, *args: str) -> None:
    subprocess.run(["git", *args], cwd=repo, check=True, capture_output=True, text=True)


class VerifyChangeCreatedTests(unittest.TestCase):
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
        self.cfg = {"created_check": "", "check_timeout_minutes": 1}

    def tearDown(self) -> None:
        self.tmp.cleanup()

    def write_authored_change(self, cid: str) -> None:
        cdir = self.repo / "openspec" / "changes" / cid
        cdir.mkdir(parents=True)
        (cdir / "proposal.md").write_text("## Why\n", encoding="utf-8")
        (cdir / "tasks.md").write_text("## 1. Tasks\n", encoding="utf-8")

    def test_create_allows_preexisting_tracked_changes(self) -> None:
        (self.repo / "tracked.txt").write_text("dirty before create\n", encoding="utf-8")
        before = self.opsx_plan.tracked_worktree_snapshot(self.repo)

        self.write_authored_change("add-example")

        ok, why = self.opsx_plan.verify_change_created(
            self.repo, self.cfg, "add-example", before
        )
        self.assertTrue(ok, why)

    def test_create_rejects_new_tracked_changes(self) -> None:
        before = self.opsx_plan.tracked_worktree_snapshot(self.repo)

        self.write_authored_change("add-example")
        (self.repo / "tracked.txt").write_text("dirty during create\n", encoding="utf-8")

        ok, why = self.opsx_plan.verify_change_created(
            self.repo, self.cfg, "add-example", before
        )
        self.assertFalse(ok)
        self.assertIn("creation modified tracked files", why)

    def test_accept_verification_ignores_unrelated_dirty_tree(self) -> None:
        (self.repo / "tracked.txt").write_text("dirty before accept\n", encoding="utf-8")
        self.write_authored_change("add-example")

        ok, why = self.opsx_plan.verify_change_created(self.repo, self.cfg, "add-example")
        self.assertTrue(ok, why)


class DirectOpenCodeExecutionTests(unittest.TestCase):
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
        self.cid = "add-example"
        self.plan_name = "direct-plan"
        self.cfg = {
            "name": self.plan_name,
            "adapter": "opencode",
            "implement_invoke": "opencode run --agent opsx-implementer",
            "review_invoke": "opencode run --agent opsx-reviewer",
            "archive_invoke": "opencode run --agent opsx-archiver",
            "invoke": 'opencode run "/opsx-drive {change}"',
            "state_file": ".opencode/opsx-controller/{change}.json",
            "timeout_minutes": 1,
            "max_attempts": 2,
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
                    "max_attempts": 2,
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
        self.write_authored_change(self.cid)
        record = self.opsx_plan.rec(self.state, self.cid)
        record["max_rounds"] = self.cfg["max_rounds"]
        record["tracked_change_files"] = self.opsx_plan.change_context_paths(
            self.repo, self.cid
        )
        self._saved_invoke = self.opsx_plan.invoke_direct_stage
        self._saved_checks = self.opsx_plan.run_fast_checks

    def tearDown(self) -> None:
        self.opsx_plan.invoke_direct_stage = self._saved_invoke
        self.opsx_plan.run_fast_checks = self._saved_checks
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
        archive_rel = f"openspec/changes/archive/2026-07-02-{cid}"
        dst = self.repo / archive_rel
        dst.parent.mkdir(parents=True, exist_ok=True)
        src.rename(dst)
        git(self.repo, "add", "-A", "openspec")
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

    def stage_runner(self, payloads: list[dict]) -> tuple[list[tuple[str, int, str]], list[str]]:
        calls: list[tuple[str, int, str]] = []
        input_blocks: list[str] = []

        def fake_invoke(repo: Path, cfg: dict, cid: str, stage: str, round_num: int, input_block: str):
            self.assertTrue(payloads, f"unexpected stage call: {stage}")
            payload = payloads.pop(0)
            self.assertEqual(stage, payload["stage"])
            calls.append((stage, round_num, cid))
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
                body = json.dumps(payload["result"]) + "\n"
            else:
                body = lines
            log_path.write_text(body, encoding="utf-8")
            return payload.get("outcome", "exited"), log_path

        self.opsx_plan.invoke_direct_stage = fake_invoke
        return calls, input_blocks

    def test_direct_run_dispatches_implement_review_archive_and_persists_logs(self) -> None:
        calls, inputs = self.stage_runner(
            [
                {
                    "stage": "implement",
                    "result": {
                        "status": "implemented",
                        "change": self.cid,
                        "round": 1,
                        "progress_made": True,
                        "completed_tasks": ["1.1"],
                        "remaining_tasks": ["1.2"],
                        "task_counts": {"complete": 1, "total": 2},
                        "files_touched": ["orchestrator/opsx-plan.py"],
                        "known_change_files": [
                            f"openspec/changes/{self.cid}/tasks.md",
                        ],
                        "summary": "implemented first round",
                        "cache_update": {
                            "change_summary": "direct execution change summary",
                            "refresh_reason": "initial direct round",
                            "source_paths": [
                                f"openspec/changes/{self.cid}/tasks.md",
                            ],
                            "scope_hint": "opsx-plan direct orchestration",
                        },
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

        self.assertEqual(result, self.opsx_plan.DONE)
        self.assertEqual([stage for stage, _, _ in calls], ["implement", "review", "archive"])
        self.assertIn(f"CHANGE: {self.cid}", inputs[0])
        self.assertIn("ROUND: 1", inputs[0])

        record = self.opsx_plan.rec(self.state, self.cid)
        self.assertEqual(record["phase"], "done")
        self.assertEqual(record["status"], self.opsx_plan.DONE)
        self.assertEqual(record["archive"]["status"], "passed")
        self.assertEqual(record["last_stage"]["name"], "archive")
        self.assertTrue(Path(record["last_stage"]["log_path"]).is_file())
        self.assertTrue(record["context_cache"]["valid"])

        worker_state = self.opsx_plan.worker_state_path(self.repo, self.plan_name, self.cid)
        self.assertTrue(worker_state.is_file())
        payload = json.loads(worker_state.read_text(encoding="utf-8"))
        self.assertEqual(payload["phase"], "done")
        self.assertEqual(payload["archive"]["status"], "passed")

    def test_parse_failure_blocks_direct_stage(self) -> None:
        self.stage_runner(
            [
                {
                    "stage": "implement",
                    "lines": "not json\nsecond line\n",
                }
            ]
        )

        result = self.opsx_plan.run_direct_change(self.repo, self.cfg, self.state, self.cid)

        self.assertEqual(result, "failed")
        record = self.opsx_plan.rec(self.state, self.cid)
        self.assertEqual(record["last_result"], "subagent_output_invalid")
        self.assertEqual(record["status"], self.opsx_plan.FAILED)
        self.assertIn("output invalid", record["reason"])

    def test_transcript_log_with_final_json_line_is_accepted(self) -> None:
        calls, _ = self.stage_runner(
            [
                {
                    "stage": "implement",
                    "lines": "\x1b[0mnoise\n$ command\n{\"status\":\"implemented\",\"change\":\"add-example\",\"round\":1,\"progress_made\":false,\"completed_tasks\":[],\"remaining_tasks\":[],\"task_counts\":{\"complete\":2,\"total\":2},\"files_touched\":[],\"known_change_files\":[],\"summary\":\"implementation complete\"}\n",
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

        self.assertEqual(result, self.opsx_plan.DONE)
        self.assertEqual([stage for stage, _, _ in calls], ["implement", "review", "archive"])

    def test_reconcile_recovers_interrupted_review_from_plan_state(self) -> None:
        record = self.opsx_plan.rec(self.state, self.cid)
        record["status"] = self.opsx_plan.RUNNING
        record["phase"] = "review"
        record["round"] = 2

        self.opsx_plan.reconcile(self.repo, self.cfg, self.state)

        record = self.opsx_plan.rec(self.state, self.cid)
        self.assertEqual(record["status"], self.opsx_plan.PENDING)
        self.assertEqual(record["phase"], "review")
        self.assertEqual(record["round"], 2)

    def test_review_failure_loops_back_to_implement_with_fix_prompt(self) -> None:
        calls, _ = self.stage_runner(
            [
                {
                    "stage": "implement",
                    "result": {
                        "status": "implemented",
                        "change": self.cid,
                        "round": 1,
                        "progress_made": True,
                        "completed_tasks": ["1.1"],
                        "remaining_tasks": ["1.2"],
                        "task_counts": {"complete": 1, "total": 2},
                        "files_touched": ["orchestrator/opsx-plan.py"],
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
                        "summary": "missing retry wiring",
                        "fix_prompt": "Re-run implement and add retry wiring tests.",
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
                        "files_touched": ["tests/orchestrator/test_opsx_plan.py"],
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

        self.assertEqual(result, self.opsx_plan.DONE)
        self.assertEqual(
            [stage for stage, _, _ in calls],
            ["implement", "review", "implement", "review", "archive"],
        )
        self.assertEqual(self.opsx_plan.rec(self.state, self.cid)["round"], 2)
        self.assertEqual(self.opsx_plan.rec(self.state, self.cid)["latest_fix_prompt"], "")

    def test_review_retry_budget_exhaustion_stops_change(self) -> None:
        self.cfg["max_rounds"] = 1
        record = self.opsx_plan.rec(self.state, self.cid)
        record["max_rounds"] = 1
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
                        "remaining_tasks": ["1.1", "1.2"],
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
                        "finding_counts": {"critical": 0, "warning": 1, "note": 0},
                        "summary": "review failed",
                        "fix_prompt": "Add missing verification coverage.",
                        "next_phase": "implement",
                    },
                },
            ]
        )

        result = self.opsx_plan.run_direct_change(self.repo, self.cfg, self.state, self.cid)

        self.assertEqual(result, "stop")
        record = self.opsx_plan.rec(self.state, self.cid)
        self.assertEqual(record["status"], self.opsx_plan.FAILED)
        self.assertEqual(record["last_result"], "max_rounds_reached")
        self.assertIn("retry budget exhausted", record["reason"])

    def test_no_progress_stops_after_two_implement_rounds(self) -> None:
        self.stage_runner(
            [
                {
                    "stage": "implement",
                    "result": {
                        "status": "implemented",
                        "change": self.cid,
                        "round": 1,
                        "progress_made": False,
                        "completed_tasks": [],
                        "remaining_tasks": ["1.1", "1.2"],
                        "task_counts": {"complete": 0, "total": 2},
                        "files_touched": [],
                        "known_change_files": [],
                        "summary": "no progress in round 1",
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
                        "summary": "still missing implementation",
                        "fix_prompt": "Implement the missing direct review loop.",
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
                        "summary": "no progress in round 2",
                    },
                },
            ]
        )

        result = self.opsx_plan.run_direct_change(self.repo, self.cfg, self.state, self.cid)

        self.assertEqual(result, "stop")
        record = self.opsx_plan.rec(self.state, self.cid)
        self.assertEqual(record["status"], self.opsx_plan.FAILED)
        self.assertEqual(record["last_result"], "no_progress")
        self.assertEqual(record["no_progress_streak"], 2)

    def test_archive_success_without_repo_evidence_does_not_complete_change(self) -> None:
        self.stage_runner(
            [
                {
                    "stage": "implement",
                    "result": {
                        "status": "implemented",
                        "change": self.cid,
                        "round": 1,
                        "progress_made": True,
                        "completed_tasks": ["1.1"],
                        "remaining_tasks": ["1.2"],
                        "task_counts": {"complete": 1, "total": 2},
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
                        "status": "archived",
                        "change": self.cid,
                        "archive_path": f"openspec/changes/archive/2026-07-02-{self.cid}",
                        "spec_sync_status": "no-delta",
                        "commit": "deadbeef",
                        "summary": "archive claimed success",
                    },
                },
            ]
        )

        result = self.opsx_plan.run_direct_change(self.repo, self.cfg, self.state, self.cid)

        self.assertEqual(result, "stop")
        record = self.opsx_plan.rec(self.state, self.cid)
        self.assertEqual(record["status"], self.opsx_plan.FAILED)
        self.assertEqual(record["archive"]["status"], "failed")
        self.assertIn("still exists", record["reason"])

    def test_archive_success_still_requires_fast_checks(self) -> None:
        self.opsx_plan.run_fast_checks = lambda repo, cfg: (False, "check failed: smoke")
        self.stage_runner(
            [
                {
                    "stage": "implement",
                    "result": {
                        "status": "implemented",
                        "change": self.cid,
                        "round": 1,
                        "progress_made": True,
                        "completed_tasks": ["1.1"],
                        "remaining_tasks": ["1.2"],
                        "task_counts": {"complete": 1, "total": 2},
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

        self.assertEqual(result, "stop")
        record = self.opsx_plan.rec(self.state, self.cid)
        self.assertEqual(record["status"], self.opsx_plan.FAILED)
        self.assertEqual(record["last_result"], "post_archive_check_failed")
        self.assertIn("post-archive", record["reason"])


class SingleChangeConfigTests(unittest.TestCase):
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

    def tearDown(self) -> None:
        self.tmp.cleanup()

    def write_authored_change(self, cid: str) -> None:
        cdir = self.repo / "openspec" / "changes" / cid
        cdir.mkdir(parents=True)
        (cdir / "proposal.md").write_text("## Why\n", encoding="utf-8")
        (cdir / "tasks.md").write_text(
            "## 1. Tasks\n\n- [ ] 1.1 Example task\n", encoding="utf-8"
        )

    def test_build_single_change_config_produces_valid_config(self) -> None:
        self.write_authored_change("add-demo")
        cfg = self.opsx_plan.build_single_change_config(self.repo, "add-demo")

        self.assertEqual(cfg["name"], "run-add-demo")
        self.assertEqual(cfg["adapter"], "opencode")
        self.assertEqual(cfg["order"], ["add-demo"])
        self.assertIn("add-demo", cfg["changes"])
        self.assertTrue(cfg["require_clean_tracked"])
        self.assertFalse(cfg["review_created"])
        self.assertTrue(
            self.opsx_plan.is_direct_opencode(cfg),
            "single-change config must route through direct workers",
        )

    def test_build_config_fails_for_missing_change_dir(self) -> None:
        with self.assertRaises(self.opsx_plan.PlanError) as ctx:
            self.opsx_plan.build_single_change_config(self.repo, "no-such-change")
        self.assertIn("does not exist", str(ctx.exception))

    def test_build_config_fails_for_incomplete_change(self) -> None:
        cdir = self.repo / "openspec" / "changes" / "missing-tasks"
        cdir.mkdir(parents=True)
        (cdir / "proposal.md").write_text("## Why\n", encoding="utf-8")

        with self.assertRaises(self.opsx_plan.PlanError) as ctx:
            self.opsx_plan.build_single_change_config(self.repo, "missing-tasks")
        self.assertIn("missing required artifacts", str(ctx.exception))

    def test_cmd_run_one_rejects_missing_change(self) -> None:
        args = argparse.Namespace(repo=str(self.repo), change="no-such-change")
        rc = self.opsx_plan.cmd_run_one(args)
        self.assertEqual(rc, 2)

    def test_cmd_run_one_rejects_unauthored_change(self) -> None:
        cdir = self.repo / "openspec" / "changes" / "bare"
        cdir.mkdir(parents=True)
        args = argparse.Namespace(repo=str(self.repo), change="bare")
        rc = self.opsx_plan.cmd_run_one(args)
        self.assertEqual(rc, 2)

    def test_cmd_run_one_rejects_dirty_tracked_worktree(self) -> None:
        self.write_authored_change("add-dirty")
        (self.repo / "tracked.txt").write_text("dirty\n", encoding="utf-8")

        args = argparse.Namespace(repo=str(self.repo), change="add-dirty")
        stderr = io.StringIO()

        with mock.patch.object(self.opsx_plan, "run_direct_change") as run_direct_change, mock.patch("sys.stderr", stderr):
            rc = self.opsx_plan.cmd_run_one(args)

        self.assertEqual(rc, 2)
        run_direct_change.assert_not_called()
        self.assertIn("tracked worktree is dirty", stderr.getvalue())


class SingleChangeRunnerTests(unittest.TestCase):
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
        self.cid = "add-single-runner"
        self.plan_name = f"run-{self.cid}"
        self._saved_invoke = self.opsx_plan.invoke_direct_stage
        self._saved_checks = self.opsx_plan.run_fast_checks

    def tearDown(self) -> None:
        self.opsx_plan.invoke_direct_stage = self._saved_invoke
        self.opsx_plan.run_fast_checks = self._saved_checks
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
        archive_rel = f"openspec/changes/archive/2026-07-02-{cid}"
        dst = self.repo / archive_rel
        dst.parent.mkdir(parents=True, exist_ok=True)
        src.rename(dst)
        git(self.repo, "add", "-A", "openspec")
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

    def stage_runner(self, payloads: list[dict]) -> tuple[list[tuple[str, int, str]], list[str]]:
        calls: list[tuple[str, int, str]] = []
        input_blocks: list[str] = []

        def fake_invoke(repo: Path, cfg: dict, cid: str, stage: str, round_num: int, input_block: str):
            self.assertTrue(payloads, f"unexpected stage call: {stage}")
            payload = payloads.pop(0)
            self.assertEqual(stage, payload["stage"])
            calls.append((stage, round_num, cid))
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
                body = json.dumps(payload["result"]) + "\n"
            else:
                body = lines
            log_path.write_text(body, encoding="utf-8")
            return payload.get("outcome", "exited"), log_path

        self.opsx_plan.invoke_direct_stage = fake_invoke
        return calls, input_blocks

    def test_single_change_runs_implement_review_archive(self) -> None:
        self.write_authored_change(self.cid)
        state = self.opsx_plan.load_state(self.repo, self.plan_name)
        cfg = self.opsx_plan.build_single_change_config(self.repo, self.cid)

        calls, _ = self.stage_runner(
            [
                {
                    "stage": "implement",
                    "result": {
                        "status": "implemented",
                        "change": self.cid,
                        "round": 1,
                        "progress_made": True,
                        "completed_tasks": ["1.1"],
                        "remaining_tasks": ["1.2"],
                        "task_counts": {"complete": 1, "total": 2},
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

        result = self.opsx_plan.run_direct_change(self.repo, cfg, state, self.cid)

        self.assertEqual(result, self.opsx_plan.DONE)
        self.assertEqual(
            [stage for stage, _, _ in calls],
            ["implement", "review", "archive"],
        )
        record = self.opsx_plan.rec(state, self.cid)
        self.assertEqual(record["phase"], "done")
        self.assertEqual(record["status"], self.opsx_plan.DONE)
        self.assertEqual(record["archive"]["status"], "passed")

    def test_single_change_review_failure_retries_implement(self) -> None:
        self.write_authored_change(self.cid)
        state = self.opsx_plan.load_state(self.repo, self.plan_name)
        cfg = self.opsx_plan.build_single_change_config(self.repo, self.cid)

        calls, _ = self.stage_runner(
            [
                {
                    "stage": "implement",
                    "result": {
                        "status": "implemented",
                        "change": self.cid,
                        "round": 1,
                        "progress_made": True,
                        "completed_tasks": ["1.1"],
                        "remaining_tasks": ["1.2"],
                        "task_counts": {"complete": 1, "total": 2},
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
                        "summary": "missing coverage",
                        "fix_prompt": "Add missing tests for the single-change runner.",
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
                        "summary": "review passed after retry",
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

        result = self.opsx_plan.run_direct_change(self.repo, cfg, state, self.cid)

        self.assertEqual(result, self.opsx_plan.DONE)
        self.assertEqual(
            [stage for stage, _, _ in calls],
            ["implement", "review", "implement", "review", "archive"],
        )
        self.assertEqual(self.opsx_plan.rec(state, self.cid)["round"], 2)
        self.assertEqual(
            self.opsx_plan.rec(state, self.cid)["latest_fix_prompt"], ""
        )

    def test_single_change_state_persists_under_run_prefix(self) -> None:
        self.write_authored_change(self.cid)
        state = self.opsx_plan.load_state(self.repo, self.plan_name)
        cfg = self.opsx_plan.build_single_change_config(self.repo, self.cid)

        self.stage_runner(
            [
                {
                    "stage": "implement",
                    "result": {
                        "status": "implemented",
                        "change": self.cid,
                        "round": 1,
                        "progress_made": True,
                        "completed_tasks": ["1.1"],
                        "remaining_tasks": [],
                        "task_counts": {"complete": 1, "total": 2},
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

        self.opsx_plan.run_direct_change(self.repo, cfg, state, self.cid)

        state_path = self.opsx_plan.state_path(self.repo, self.plan_name)
        self.assertTrue(state_path.is_file(), f"expected state at {state_path}")

        worker_state = self.opsx_plan.worker_state_path(self.repo, self.plan_name, self.cid)
        self.assertTrue(worker_state.is_file())


class MainDispatchTests(unittest.TestCase):
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
        self.cid = "add-cli-dispatch"

    def tearDown(self) -> None:
        self.tmp.cleanup()

    def write_authored_change(self, cid: str) -> None:
        cdir = self.repo / "openspec" / "changes" / cid
        cdir.mkdir(parents=True)
        (cdir / "proposal.md").write_text("## Why\n", encoding="utf-8")
        (cdir / "tasks.md").write_text(
            "## 1. Tasks\n\n- [ ] 1.1 Example task\n", encoding="utf-8"
        )

    def test_main_dispatches_opsx_run_executable_into_single_change_runner(self) -> None:
        self.write_authored_change(self.cid)
        calls: list[tuple[Path, str, str]] = []

        def fake_run_direct_change(
            repo: Path,
            cfg: dict,
            state: dict,
            cid: str,
            budget_deadline: float | None = None,
        ) -> str:
            self.assertIsNone(budget_deadline)
            calls.append((repo, cfg["name"], cid))
            return self.opsx_plan.DONE

        with mock.patch.object(
            self.opsx_plan, "run_direct_change", side_effect=fake_run_direct_change
        ) as run_direct_change, mock.patch.object(
            self.opsx_plan.sys,
            "argv",
            ["opsx-run", self.cid, "--repo", str(self.repo)],
        ):
            rc = self.opsx_plan.main()

        self.assertEqual(rc, 0)
        run_direct_change.assert_called_once()
        self.assertEqual(calls, [(self.repo.resolve(), f"run-{self.cid}", self.cid)])

    def test_main_reports_spawn_error_from_opsx_run(self) -> None:
        self.write_authored_change(self.cid)
        stderr = io.StringIO()

        def fake_run_direct_change(
            repo: Path,
            cfg: dict,
            state: dict,
            cid: str,
            budget_deadline: float | None = None,
        ) -> str:
            self.assertEqual(repo, self.repo.resolve())
            self.assertIsNone(budget_deadline)
            record = self.opsx_plan.rec(state, cid)
            record["phase"] = "implement"
            self.opsx_plan.set_status(
                state,
                cid,
                self.opsx_plan.FAILED,
                f"could not spawn implement: {cfg['implement_invoke']}",
            )
            return "spawn_error"

        with mock.patch.object(
            self.opsx_plan,
            "run_direct_change",
            side_effect=fake_run_direct_change,
        ) as run_direct_change, mock.patch.object(
            self.opsx_plan.sys,
            "argv",
            ["opsx-run", self.cid, "--repo", str(self.repo)],
        ), mock.patch("sys.stderr", stderr):
            rc = self.opsx_plan.main()

        self.assertEqual(rc, 2)
        run_direct_change.assert_called_once()
        self.assertIn("could not start direct worker dispatch", stderr.getvalue())
        self.assertIn(f"openspec/changes/{self.cid}", stderr.getvalue())
        self.assertIn(
            self.opsx_plan.ADAPTER_DEFAULTS["opencode"]["implement_invoke"],
            stderr.getvalue(),
        )
        self.assertNotIn(
            self.opsx_plan.ADAPTER_DEFAULTS["opencode"]["invoke"],
            stderr.getvalue(),
        )

    def test_main_rejects_extra_opsx_run_positionals_without_worker_dispatch(self) -> None:
        stderr = io.StringIO()

        with mock.patch.object(self.opsx_plan, "run_direct_change") as run_direct_change, mock.patch.object(
            self.opsx_plan.sys,
            "argv",
            ["opsx-run", self.cid, "extra", "--repo", str(self.repo)],
        ), mock.patch("sys.stderr", stderr):
            rc = self.opsx_plan.main()

        self.assertEqual(rc, 2)
        run_direct_change.assert_not_called()
        self.assertIn("unexpected argument: extra", stderr.getvalue())

    def test_main_parses_run_one_subcommand_and_calls_cmd_run_one(self) -> None:
        calls: list[argparse.Namespace] = []

        def fake_cmd_run_one(args: argparse.Namespace) -> int:
            calls.append(args)
            return 37

        with mock.patch.object(
            self.opsx_plan, "cmd_run_one", side_effect=fake_cmd_run_one
        ) as cmd_run_one, mock.patch.object(
            self.opsx_plan.sys,
            "argv",
            ["opsx-plan", "--repo", str(self.repo), "run-one", self.cid],
        ):
            rc = self.opsx_plan.main()

        self.assertEqual(rc, 37)
        cmd_run_one.assert_called_once()
        self.assertEqual(len(calls), 1)
        self.assertEqual(calls[0].repo, str(self.repo))
        self.assertEqual(calls[0].change, self.cid)


class OpsxDriveCompatibilityTests(unittest.TestCase):
    CMD = Path(__file__).resolve().parents[2] / "adapters" / "opencode" / "commands" / "opsx-drive.md"

    def _cmd_text(self) -> str:
        return self.CMD.read_text(encoding="utf-8")

    def _frontmatter(self, text: str) -> dict[str, bool]:
        fm: dict[str, bool] = {}
        for line in text.splitlines():
            if ":" not in line:
                continue
            key, _, val = line.partition(":")
            fm[key.strip()] = val.strip() == "true" or val.strip() == "false" or bool(val.strip())
        return fm

    def test_opsx_drive_command_surface_remains_manual_entrypoint(self) -> None:
        self.assertTrue(self.CMD.is_file(), f"command surface not found: {self.CMD}")
        text = self._cmd_text()

        self.assertIn("agent: opsx-controller", text,
                      "opsx-drive must use the opsx-controller agent")
        self.assertIn("subtask: false", text,
                      "opsx-drive must not be marked as a subtask")

        self.assertIn("manual single-change", text.lower(),
                      "opsx-drive must document itself as the manual single-change surface")

    def test_opsx_plan_routes_opencode_through_direct_workers_not_opsx_drive(self) -> None:
        self.opsx_plan = load_opsx_plan()

        defaults = self.opsx_plan.ADAPTER_DEFAULTS["opencode"]
        self.assertIn(
            "/opsx-drive", defaults.get("invoke", ""),
            "ADAPTER_DEFAULTS must preserve opsx-drive invoke for legacy surface",
        )

        cfg = {"adapter": "opencode", **defaults}
        self.assertTrue(
            self.opsx_plan.is_direct_opencode(cfg),
            "default OpenCode config must route through direct workers, not /opsx-drive",
        )


class OpenCodeAgentModeTests(unittest.TestCase):
    AGENT_DIR = Path(__file__).resolve().parents[2] / "adapters" / "opencode" / "agents"

    def test_opencode_worker_agents_are_runnable_via_run_agent(self) -> None:
        for name in (
            "opsx-controller.md",
            "opsx-implementer.md",
            "opsx-reviewer.md",
            "opsx-archiver.md",
        ):
            text = (self.AGENT_DIR / name).read_text(encoding="utf-8")
            self.assertIn(
                "mode: all",
                text,
                f"{name} must remain runnable both as a direct --agent target and as a subagent",
            )

    def test_opencode_worker_agents_expand_home_and_activate_repo_venv(self) -> None:
        for name in (
            "opsx-implementer.md",
            "opsx-reviewer.md",
            "opsx-archiver.md",
        ):
            text = (self.AGENT_DIR / name).read_text(encoding="utf-8")
            self.assertIn(
                "Expand `$HOME` before reading; never pass a literal `$HOME/...` path",
                text,
                f"{name} must forbid literal $HOME Read paths",
            )
            self.assertIn(
                "If `.venv/bin/activate` exists at the repo root, activate it",
                text,
                f"{name} must remind the worker to activate the repo venv when present",
            )
            self.assertIn(
                "Do not use Glob for this step; try exact Read paths",
                text,
                f"{name} must avoid broad globbing for global prompt discovery",
            )

        for name in ("opsx-reviewer.md", "opsx-archiver.md"):
            text = (self.AGENT_DIR / name).read_text(encoding="utf-8")
            self.assertIn(
                '"~/.config/opencode/**": allow',
                text,
                f"{name} must allow direct reads under ~/.config/opencode",
            )


if __name__ == "__main__":
    unittest.main()
