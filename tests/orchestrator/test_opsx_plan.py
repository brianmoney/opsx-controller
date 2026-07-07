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

    def test_archive_success_is_rejected_when_tracked_tree_stays_dirty(self) -> None:
        self.cfg["require_clean_tracked"] = True

        def fake_invoke(repo: Path, cfg: dict, cid: str, stage: str, round_num: int, input_block: str):
            log_path = self.opsx_plan.next_stage_log_path(repo, cid, stage, round_num)
            log_path.parent.mkdir(parents=True, exist_ok=True)
            if stage == "implement":
                payload = {
                    "status": "implemented",
                    "change": cid,
                    "round": round_num,
                    "progress_made": True,
                    "completed_tasks": ["1.1"],
                    "remaining_tasks": ["1.2"],
                    "task_counts": {"complete": 1, "total": 2},
                    "files_touched": ["orchestrator/opsx-plan.py"],
                    "known_change_files": [f"openspec/changes/{cid}/tasks.md"],
                    "summary": "implemented first round",
                }
            elif stage == "review":
                payload = {
                    "status": "reviewed",
                    "change": cid,
                    "round": round_num,
                    "verdict": "pass",
                    "finding_counts": {"critical": 0, "warning": 0, "note": 0},
                    "summary": "review passed",
                    "fix_prompt": "",
                    "next_phase": "archive",
                }
            else:
                archive_path, commit = self.archive_change_in_repo(cid)
                (repo / "tracked.txt").write_text("dirty after archive\n", encoding="utf-8")
                payload = {
                    "status": "archived",
                    "change": cid,
                    "archive_path": archive_path,
                    "spec_sync_status": "no-delta",
                    "commit": commit,
                    "summary": "archive succeeded",
                }
            log_path.write_text(json.dumps(payload) + "\n", encoding="utf-8")
            return "exited", log_path

        self.opsx_plan.invoke_direct_stage = fake_invoke

        result = self.opsx_plan.run_direct_change(self.repo, self.cfg, self.state, self.cid)

        self.assertEqual(result, "stop")
        record = self.opsx_plan.rec(self.state, self.cid)
        self.assertEqual(record["status"], self.opsx_plan.FAILED)
        self.assertEqual(record["archive"]["status"], "failed")
        self.assertEqual(record["last_result"], "post_archive_dirty_tracked")
        self.assertIn("post-archive tracked worktree is dirty", record["reason"])

    def test_reconcile_keeps_done_change_when_newer_archive_prefix_commit_exists(self) -> None:
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
                        "files_touched": ["orchestrator/opsx-plan.py"],
                        "known_change_files": [f"openspec/changes/{self.cid}/tasks.md"],
                        "summary": "implemented first round",
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

        archived_tasks = (
            self.repo
            / "openspec"
            / "changes"
            / "archive"
            / f"2026-07-02-{self.cid}"
            / "tasks.md"
        )
        archived_tasks.write_text("## 1. Tasks\n\n- [x] 1.1 Example task\n", encoding="utf-8")
        git(self.repo, "add", str(archived_tasks.relative_to(self.repo)))
        git(
            self.repo,
            "-c",
            "user.email=test@example.invalid",
            "-c",
            "user.name=Test User",
            "commit",
            "-m",
            f"archive({self.cid}): follow-up archive cleanup",
        )

        self.opsx_plan.reconcile(self.repo, self.cfg, self.state)

        record = self.opsx_plan.rec(self.state, self.cid)
        self.assertEqual(record["status"], self.opsx_plan.DONE)
        self.assertEqual(record["phase"], "done")
        ok, why = self.opsx_plan.verify_direct_archive_done(self.repo, self.cid, record)
        self.assertTrue(ok, why)

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

    @staticmethod
    def _extract_external_directory_block(text: str) -> str:
        lines = text.splitlines()
        start_idx: int | None = None
        base_indent: int | None = None
        for i, line in enumerate(lines):
            stripped = line.lstrip()
            if stripped.startswith("external_directory:"):
                start_idx = i
                base_indent = len(line) - len(stripped)
                break
        if start_idx is None:
            return ""
        block_lines: list[str] = []
        for j in range(start_idx + 1, len(lines)):
            line = lines[j]
            stripped = line.lstrip()
            if stripped == "":
                block_lines.append(line)
                continue
            indent = len(line) - len(stripped)
            if indent <= base_indent and not stripped.startswith('"') and not stripped.startswith("#"):
                break
            block_lines.append(line)
        return "\n".join(block_lines)

    def test_opencode_worker_agents_deny_broad_external_directory(self) -> None:
        for name in (
            "opsx-controller.md",
            "opsx-implementer.md",
            "opsx-reviewer.md",
            "opsx-archiver.md",
        ):
            text = (self.AGENT_DIR / name).read_text(encoding="utf-8")
            block = self._extract_external_directory_block(text)
            self.assertTrue(
                block,
                f"{name} must contain an external_directory permission block",
            )
            self.assertIn(
                '"*": deny',
                block,
                f"{name} must deny broad external_directory access (wildcard deny inside external_directory block)",
            )
            self.assertIn(
                "~/.config/opencode",
                block,
                f"{name} must preserve explicit ~/.config/opencode allow rules inside the external_directory block",
            )


class ParseStageJsonPermissionTests(unittest.TestCase):
    def setUp(self) -> None:
        self.opsx_plan = load_opsx_plan()
        self.tmp = tempfile.TemporaryDirectory()
        self.log_dir = Path(self.tmp.name)
        self.log_dir.mkdir(parents=True, exist_ok=True)

    def tearDown(self) -> None:
        self.tmp.cleanup()

    def _write_log(self, content: str) -> Path:
        p = self.log_dir / f"test-{id(content)}.log"
        p.write_text(content, encoding="utf-8")
        return p

    def test_auto_rejected_external_directory_transcript_is_parsed_as_permission_denial(self) -> None:
        content = (
            "# some header\n"
            "model output line\n"
            "The user rejected permission for external_directory\n"
            "auto-rejecting request\n"
        )
        log_path = self._write_log(content)
        payload, reason = self.opsx_plan.parse_stage_json(log_path)
        self.assertIsNone(payload)
        self.assertIn("permission denied before JSON output", reason)

    def test_valid_final_json_remains_authoritative_despite_noisy_transcript(self) -> None:
        content = (
            "# some header\n"
            "permission requested for external_directory\n"
            "auto-rejecting\n"
            '{"status":"implemented","change":"ex","round":1,"progress_made":true,'
            '"completed_tasks":[],"remaining_tasks":[],'
            '"task_counts":{"complete":0,"total":0},'
            '"files_touched":[],"known_change_files":[],"summary":"done"}\n'
        )
        log_path = self._write_log(content)
        payload, reason = self.opsx_plan.parse_stage_json(log_path)
        self.assertIsNotNone(payload, f"should have parsed JSON, got reason={reason}")
        self.assertEqual(payload["status"], "implemented")
        self.assertEqual(reason, "")

    def test_external_directory_permission_denied_marker_detected(self) -> None:
        content = (
            "# start\n"
            "external_directory permission denied for path /home/user\n"
            "aborting\n"
        )
        log_path = self._write_log(content)
        payload, reason = self.opsx_plan.parse_stage_json(log_path)
        self.assertIsNone(payload)
        self.assertIn("permission denied before JSON output", reason)
        self.assertIn("external_directory permission denied", reason)

    def test_no_permission_marker_returns_generic_reason(self) -> None:
        content = (
            "some output\n"
            "more output\n"
            "nothing parseable\n"
        )
        log_path = self._write_log(content)
        payload, reason = self.opsx_plan.parse_stage_json(log_path)
        self.assertIsNone(payload)
        self.assertIn("expected a final JSON object line", reason)
        self.assertNotIn("permission denied", reason)


class DirectStageTelemetryTests(unittest.TestCase):
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
        self.cid = "add-telemetry-test"
        self.plan_name = f"run-{self.cid}"
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
        archive_rel = f"openspec/changes/archive/2026-07-05-{cid}"
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
        record = self.opsx_plan.rec(self.state, self.cid)
        record["max_rounds"] = self.cfg["max_rounds"]
        record["tracked_change_files"] = self.opsx_plan.change_context_paths(
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
                        "completed_tasks": ["1.1"],
                        "remaining_tasks": ["1.2"],
                        "task_counts": {"complete": 1, "total": 2},
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
        self.assertEqual(r["schema_version"], self.opsx_plan.TELEMETRY_SCHEMA_VERSION)
        self.assertTrue(r["uid"])
        self.assertIsNotNone(r["started_at"])
        self.assertIn("log_path", r["result"])
        self.assertIsNotNone(r["result"]["log_path"])

    # 6.2
    def test_successful_review_stage_populates_verdict_and_findings(self) -> None:
        self.write_authored_change(self.cid)
        record = self.opsx_plan.rec(self.state, self.cid)
        record["max_rounds"] = self.cfg["max_rounds"]
        record["tracked_change_files"] = self.opsx_plan.change_context_paths(
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
                        "completed_tasks": ["1.1"],
                        "remaining_tasks": ["1.2"],
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
        record = self.opsx_plan.rec(self.state, self.cid)
        record["max_rounds"] = self.cfg["max_rounds"]
        record["tracked_change_files"] = self.opsx_plan.change_context_paths(
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
                        "completed_tasks": ["1.1"],
                        "remaining_tasks": ["1.2"],
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
        record = self.opsx_plan.rec(self.state, self.cid)
        record["max_rounds"] = self.cfg["max_rounds"]
        record["tracked_change_files"] = self.opsx_plan.change_context_paths(
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
        record = self.opsx_plan.rec(self.state, self.cid)
        record["max_rounds"] = self.cfg["max_rounds"]
        record["tracked_change_files"] = self.opsx_plan.change_context_paths(
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
        self.write_authored_change(self.cid)
        record = self.opsx_plan.rec(self.state, self.cid)
        record["max_rounds"] = self.cfg["max_rounds"]
        record["tracked_change_files"] = self.opsx_plan.change_context_paths(
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

    # 6.7
    def test_telemetry_record_appended_to_correct_plan_jsonl(self) -> None:
        self.write_authored_change(self.cid)
        record = self.opsx_plan.rec(self.state, self.cid)
        record["max_rounds"] = self.cfg["max_rounds"]
        record["tracked_change_files"] = self.opsx_plan.change_context_paths(
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
                        "completed_tasks": ["1.1"],
                        "remaining_tasks": ["1.2"],
                        "task_counts": {"complete": 1, "total": 2},
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
        record = self.opsx_plan.rec(self.state, self.cid)
        record["max_rounds"] = self.cfg["max_rounds"]
        record["tracked_change_files"] = self.opsx_plan.change_context_paths(
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
                        "completed_tasks": ["1.1"],
                        "remaining_tasks": ["1.2"],
                        "task_counts": {"complete": 1, "total": 2},
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
        record = self.opsx_plan.rec(self.state, self.cid)
        record["max_rounds"] = self.cfg["max_rounds"]
        record["tracked_change_files"] = self.opsx_plan.change_context_paths(
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
                        "completed_tasks": ["1.1"],
                        "remaining_tasks": ["1.2"],
                        "task_counts": {"complete": 1, "total": 2},
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
        record = self.opsx_plan.rec(self.state, self.cid)
        record["max_rounds"] = self.cfg["max_rounds"]
        record["tracked_change_files"] = self.opsx_plan.change_context_paths(
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
                        "completed_tasks": ["1.1"],
                        "remaining_tasks": ["1.2"],
                        "task_counts": {"complete": 1, "total": 2},
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
        record = self.opsx_plan.rec(self.state, self.cid)
        record["max_rounds"] = self.cfg["max_rounds"]
        record["tracked_change_files"] = self.opsx_plan.change_context_paths(
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
                        "completed_tasks": ["1.1"],
                        "remaining_tasks": ["1.2"],
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
        record = self.opsx_plan.rec(self.state, self.cid)
        record["max_rounds"] = self.cfg["max_rounds"]
        record["tracked_change_files"] = self.opsx_plan.change_context_paths(
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
                        "completed_tasks": ["1.1"],
                        "remaining_tasks": ["1.2"],
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

        self.assertEqual(result, self.opsx_plan.DONE)
        record = self.opsx_plan.rec(self.state, self.cid)
        self.assertEqual(record["phase"], "done")
        self.assertEqual(record["round"], 2)
        self.assertEqual(record["status"], self.opsx_plan.DONE)
        self.assertEqual(record["archive"]["status"], "passed")

        # Verify telemetry was written for all stages
        records = self._read_telemetry()
        stages = [r["stage"] for r in records]
        self.assertEqual(stages, ["implement", "review", "implement", "review", "archive"])

    def test_blocked_implement_produces_failed_telemetry(self) -> None:
        self.write_authored_change(self.cid)
        record = self.opsx_plan.rec(self.state, self.cid)
        record["max_rounds"] = self.cfg["max_rounds"]
        record["tracked_change_files"] = self.opsx_plan.change_context_paths(
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
        record = self.opsx_plan.rec(self.state, self.cid)
        record["max_rounds"] = self.cfg["max_rounds"]
        record["tracked_change_files"] = self.opsx_plan.change_context_paths(
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
        record = self.opsx_plan.rec(self.state, self.cid)
        record["max_rounds"] = self.cfg["max_rounds"]
        record["tracked_change_files"] = self.opsx_plan.change_context_paths(
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
                        "completed_tasks": ["1.1"],
                        "remaining_tasks": ["1.2"],
                        "task_counts": {"complete": 1, "total": 2},
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
        record = self.opsx_plan.rec(self.state, self.cid)
        record["max_rounds"] = self.cfg["max_rounds"]
        record["tracked_change_files"] = self.opsx_plan.change_context_paths(
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
                        "completed_tasks": ["1.1"],
                        "remaining_tasks": ["1.2"],
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
        record = self.opsx_plan.rec(self.state, self.cid)
        record["max_rounds"] = self.cfg["max_rounds"]
        record["tracked_change_files"] = self.opsx_plan.change_context_paths(
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
                        "completed_tasks": ["1.1"],
                        "remaining_tasks": ["1.2"],
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
        record = self.opsx_plan.rec(self.state, self.cid)
        record["max_rounds"] = self.cfg["max_rounds"]
        record["tracked_change_files"] = self.opsx_plan.change_context_paths(
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
                        "completed_tasks": ["1.1"],
                        "remaining_tasks": ["1.2"],
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
        record = self.opsx_plan.rec(self.state, self.cid)
        record["max_rounds"] = self.cfg["max_rounds"]
        record["tracked_change_files"] = self.opsx_plan.change_context_paths(
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
                        "completed_tasks": ["1.1"],
                        "remaining_tasks": ["1.2"],
                        "task_counts": {"complete": 1, "total": 2},
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
            self.opsx_plan, "write_telemetry_record", side_effect=OSError("disk full")
        ), mock.patch.object(self.opsx_plan, "log", side_effect=capture_log):
            self.opsx_plan.run_direct_change(self.repo, self.cfg, self.state, self.cid)

        # Stage must still advance despite telemetry write failure
        record = self.opsx_plan.rec(self.state, self.cid)
        self.assertEqual(record["status"], self.opsx_plan.FAILED)

        # A warning must have been logged
        warning_msgs = [msg for msg in log_calls if "warning" in msg.lower()]
        self.assertTrue(warning_msgs, f"expected warning log, got: {log_calls}")

    def test_no_progress_produces_failed_telemetry(self) -> None:
        self.write_authored_change(self.cid)
        record = self.opsx_plan.rec(self.state, self.cid)
        record["max_rounds"] = self.cfg["max_rounds"]
        record["tracked_change_files"] = self.opsx_plan.change_context_paths(
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
                        "completed_tasks": [],
                        "remaining_tasks": ["1.1", "1.2"],
                        "task_counts": {"complete": 0, "total": 2},
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
        record = self.opsx_plan.rec(self.state, self.cid)
        self.cfg["max_rounds"] = 1
        record["max_rounds"] = 1
        record["tracked_change_files"] = self.opsx_plan.change_context_paths(
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
        record = self.opsx_plan.rec(self.state, self.cid)
        record["max_rounds"] = self.cfg["max_rounds"]
        record["tracked_change_files"] = self.opsx_plan.change_context_paths(
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
                        "completed_tasks": ["1.1"],
                        "remaining_tasks": ["1.2"],
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
        record = self.opsx_plan.rec(self.state, self.cid)
        record["max_rounds"] = self.cfg["max_rounds"]
        record["tracked_change_files"] = self.opsx_plan.change_context_paths(
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
        record = self.opsx_plan.rec(self.state, self.cid)
        record["max_rounds"] = self.cfg["max_rounds"]
        record["tracked_change_files"] = self.opsx_plan.change_context_paths(
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
                        "completed_tasks": ["1.1"],
                        "remaining_tasks": ["1.2"],
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
        record = self.opsx_plan.rec(self.state, self.cid)
        record["max_rounds"] = self.cfg["max_rounds"]
        record["tracked_change_files"] = self.opsx_plan.change_context_paths(
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
                        "completed_tasks": ["1.1"],
                        "remaining_tasks": ["1.2"],
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

        result = self.opsx_plan.run_direct_change(self.repo, self.cfg, self.state, self.cid)
        self.assertEqual(result, self.opsx_plan.DONE)
        self._assert_worker_state_has_latest_telemetry_uid()

    def test_terminal_no_progress_persists_telemetry_uid_to_worker_state(self) -> None:
        """No-progress stop must also persist its telemetry UID."""
        self.write_authored_change(self.cid)
        record = self.opsx_plan.rec(self.state, self.cid)
        record["max_rounds"] = self.cfg["max_rounds"]
        record["tracked_change_files"] = self.opsx_plan.change_context_paths(
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
                        "completed_tasks": [],
                        "remaining_tasks": ["1.1", "1.2"],
                        "task_counts": {"complete": 0, "total": 2},
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
        record = self.opsx_plan.rec(self.state, self.cid)
        self.cfg["max_rounds"] = 1
        record["max_rounds"] = 1
        record["tracked_change_files"] = self.opsx_plan.change_context_paths(
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
        record = self.opsx_plan.rec(self.state, self.cid)
        record["max_rounds"] = self.cfg["max_rounds"]
        record["tracked_change_files"] = self.opsx_plan.change_context_paths(
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
                        "completed_tasks": ["1.1"],
                        "remaining_tasks": ["1.2"],
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


class CompileTests(unittest.TestCase):
    """Tests for ``opsx-plan compile``: prompt construction, template
    injection, validation, error handling, and CLI routing."""

    def setUp(self) -> None:
        self.opsx_plan = load_opsx_plan()
        self.tmp = tempfile.TemporaryDirectory()
        self.repo = Path(self.tmp.name)
        git(self.repo, "init")
        git(
            self.repo,
            "-c",
            "user.email=test@example.invalid",
            "-c",
            "user.name=Test User",
            "commit",
            "-m",
            "init",
            "--allow-empty",
        )

    def tearDown(self) -> None:
        self.tmp.cleanup()

    def _write_plan_md(self, rel_path: str, content: str) -> Path:
        p = self.repo / rel_path
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(content, encoding="utf-8")
        return p

    def _set_model(self) -> None:
        import os as _os
        _os.environ["OPSX_CONTROLLER_MODEL"] = "test-model"

    def _clear_model(self) -> None:
        import os as _os
        _os.environ.pop("OPSX_CONTROLLER_MODEL", None)

    # -- resolve / validation helpers (already covered by earlier tasks) --

    def test_resolve_compile_source_rejects_missing_file(self) -> None:
        with self.assertRaises(self.opsx_plan.PlanError) as ctx:
            self.opsx_plan.resolve_compile_source(self.repo, "nonexistent.md")
        self.assertIn("not found", str(ctx.exception))

    def test_resolve_compile_source_rejects_non_md_extension(self) -> None:
        p = self.repo / "plan.txt"
        p.write_text("text", encoding="utf-8")
        with self.assertRaises(self.opsx_plan.PlanError) as ctx:
            self.opsx_plan.resolve_compile_source(self.repo, "plan.txt")
        self.assertIn("must be a markdown file", str(ctx.exception))

    def test_resolve_compile_output_refuses_existing_without_force(self) -> None:
        p = self.repo / "out.toml"
        p.write_text("existing", encoding="utf-8")
        with self.assertRaises(self.opsx_plan.PlanError) as ctx:
            self.opsx_plan.resolve_compile_output(self.repo, "out.toml", force=False)
        self.assertIn("exists", str(ctx.exception))
        self.assertIn("--force", str(ctx.exception))

    def test_resolve_compile_output_allows_overwrite_with_force(self) -> None:
        p = self.repo / "out.toml"
        p.write_text("existing", encoding="utf-8")
        result = self.opsx_plan.resolve_compile_output(self.repo, "out.toml", force=True)
        self.assertEqual(result, p.resolve())

    def test_check_controller_model_fails_when_unset(self) -> None:
        self._clear_model()
        with self.assertRaises(self.opsx_plan.PlanError) as ctx:
            self.opsx_plan.check_controller_model()
        self.assertIn("OPSX_CONTROLLER_MODEL", str(ctx.exception))

    def test_check_controller_model_succeeds_when_set(self) -> None:
        self._set_model()
        model = self.opsx_plan.check_controller_model()
        self.assertEqual(model, "test-model")

    # -- prompt construction --

    def test_build_compile_prompt_includes_source_content(self) -> None:
        content = "# My Plan\n\n## Phase 1\n\n### Change: `my-change`\n\n**Depends on:** None.\n"
        prompt = self.opsx_plan.build_compile_prompt(content, Path("/tmp/fake.md"), self.repo)
        self.assertIn("My Plan", prompt)
        self.assertIn("my-change", prompt)
        self.assertIn("Source plan markdown", prompt)

    def test_build_compile_prompt_includes_schema_guidance(self) -> None:
        prompt = self.opsx_plan.build_compile_prompt("content", Path("/tmp/fake.md"), self.repo)
        self.assertIn("[plan]", prompt)
        self.assertIn("[[changes]]", prompt)
        self.assertIn("depends_on", prompt)
        self.assertIn("pause_before", prompt)

    def test_build_compile_prompt_instructs_toml_only_output(self) -> None:
        prompt = self.opsx_plan.build_compile_prompt("content", Path("/tmp/fake.md"), self.repo)
        self.assertIn("Output only TOML", prompt)
        self.assertIn("fenced ```toml block", prompt)

    def test_build_compile_prompt_includes_dependency_semantics(self) -> None:
        prompt = self.opsx_plan.build_compile_prompt("content", Path("/tmp/fake.md"), self.repo)
        self.assertIn("become `depends_on`", prompt)
        self.assertIn("independence wording", prompt)
        self.assertIn("deferred", prompt.lower())

    def test_build_compile_prompt_instructs_plan_doc_reference(self) -> None:
        prompt = self.opsx_plan.build_compile_prompt("content", Path("/tmp/fake.md"), self.repo)
        self.assertIn("plan_doc", prompt)
        self.assertIn("/tmp/fake.md", prompt)

    def test_build_compile_prompt_includes_repo_relative_source_path(self) -> None:
        source = self._write_plan_md("openspec/plans/my-plan.md", "# Plan\n\n## Phase 1\n\n### Change: `c1`\n\n**Depends on:** None.\n")
        prompt = self.opsx_plan.build_compile_prompt("# Plan\n", source, self.repo)
        self.assertIn('"openspec/plans/my-plan.md"', prompt)
        self.assertIn("plan_doc", prompt)

    def test_build_compile_prompt_notes_no_templates_when_none_found(self) -> None:
        prompt = self.opsx_plan.build_compile_prompt("content", Path("/tmp/fake.md"), self.repo)
        self.assertIn("No `openspec/plans/*.md` template plan pairs were found", prompt)

    def test_build_compile_prompt_injects_template_pairs(self) -> None:
        plans_dir = self.repo / "openspec" / "plans"
        plans_dir.mkdir(parents=True)
        (plans_dir / "example-plan.md").write_text("# Example plan\n", encoding="utf-8")
        (plans_dir / "example-plan.toml").write_text('[plan]\nname = "example"\n', encoding="utf-8")

        prompt = self.opsx_plan.build_compile_prompt("content", Path("/tmp/fake.md"), self.repo)
        self.assertIn("Example plan", prompt)
        self.assertIn("example-plan.toml", prompt)
        self.assertIn('name = "example"', prompt)

    def test_discover_template_pairs_returns_empty_when_no_plans_dir(self) -> None:
        pairs = self.opsx_plan.discover_template_pairs(self.repo)
        self.assertEqual(pairs, [])

    def test_discover_template_pairs_finds_md_and_toml(self) -> None:
        plans_dir = self.repo / "openspec" / "plans"
        plans_dir.mkdir(parents=True)
        (plans_dir / "a.md").write_text("md", encoding="utf-8")
        (plans_dir / "a.toml").write_text("toml", encoding="utf-8")
        (plans_dir / "b.md").write_text("md2", encoding="utf-8")

        pairs = self.opsx_plan.discover_template_pairs(self.repo)
        self.assertEqual(len(pairs), 2)
        self.assertEqual(pairs[0][0].name, "a.md")
        self.assertIsNotNone(pairs[0][1])
        self.assertEqual(pairs[1][0].name, "b.md")
        self.assertIsNone(pairs[1][1])

    # -- cmd_compile error handling --

    def test_cmd_compile_fails_without_model(self) -> None:
        self._clear_model()
        source = self._write_plan_md("plan.md", "# Plan\n\n## Phase 1\n\n### Change: `c1`\n\n**Depends on:** None.\n")
        out = self.repo / "out.toml"
        args = argparse.Namespace(repo=str(self.repo), source="plan.md",
                                  output=str(out), force=False)
        with self.assertRaises(self.opsx_plan.PlanError) as ctx:
            self.opsx_plan.cmd_compile(args)
        self.assertIn("OPSX_CONTROLLER_MODEL", str(ctx.exception))

    def test_cmd_compile_fails_when_output_exists_without_force(self) -> None:
        self._set_model()
        source = self._write_plan_md("plan.md", "# Plan\n\n## Phase 1\n\n### Change: `c1`\n\n**Depends on:** None.\n")
        out = self.repo / "out.toml"
        out.write_text("existing", encoding="utf-8")
        args = argparse.Namespace(repo=str(self.repo), source="plan.md",
                                  output=str(out), force=False)
        with self.assertRaises(self.opsx_plan.PlanError) as ctx:
            self.opsx_plan.cmd_compile(args)
        self.assertIn("exists", str(ctx.exception))

    def test_cmd_compile_fails_when_source_not_found(self) -> None:
        self._set_model()
        out = self.repo / "out.toml"
        args = argparse.Namespace(repo=str(self.repo), source="missing.md",
                                  output=str(out), force=False)
        with self.assertRaises(self.opsx_plan.PlanError) as ctx:
            self.opsx_plan.cmd_compile(args)
        self.assertIn("not found", str(ctx.exception))

    # -- successful compile (mocked opencode) --

    def test_cmd_compile_success_with_valid_toml(self) -> None:
        self._set_model()
        source = self._write_plan_md("plan.md", "# Plan\n\n## Phase 1\n\n### Change: `c1`\n\n**Depends on:** None.\n")

        valid_toml = (
            '[plan]\nname = "test"\nadapter = "opencode"\n\n'
            "[[changes]]\nid = \"c1\"\nphase = 1\n"
        )

        def fake_run(repo, model, prompt):
            return valid_toml, ""

        original = self.opsx_plan.run_opencode_for_compile
        try:
            self.opsx_plan.run_opencode_for_compile = fake_run
            out = self.repo / "out.toml"
            args = argparse.Namespace(repo=str(self.repo), source="plan.md",
                                      output=str(out), force=False)
            rc = self.opsx_plan.cmd_compile(args)
            self.assertEqual(rc, 0)
            self.assertTrue(out.is_file())
            content = out.read_text(encoding="utf-8")
            self.assertIn("c1", content)
        finally:
            self.opsx_plan.run_opencode_for_compile = original

    def test_cmd_compile_success_with_fenced_toml(self) -> None:
        self._set_model()
        source = self._write_plan_md("plan.md", "# Plan\n\n## Phase 1\n\n### Change: `c1`\n\n**Depends on:** None.\n")

        fenced_toml = (
            '```toml\n'
            '[plan]\nname = "test"\nadapter = "opencode"\n\n'
            "[[changes]]\nid = \"c1\"\nphase = 1\n"
            '```\n'
        )

        def fake_run(repo, model, prompt):
            return fenced_toml, ""

        original = self.opsx_plan.run_opencode_for_compile
        try:
            self.opsx_plan.run_opencode_for_compile = fake_run
            out = self.repo / "out.toml"
            args = argparse.Namespace(repo=str(self.repo), source="plan.md",
                                      output=str(out), force=False)
            rc = self.opsx_plan.cmd_compile(args)
            self.assertEqual(rc, 0)
            self.assertTrue(out.is_file())
            content = out.read_text(encoding="utf-8")
            self.assertIn("c1", content)
        finally:
            self.opsx_plan.run_opencode_for_compile = original

    # -- invalid TOML rejection --

    def test_cmd_compile_rejects_invalid_toml(self) -> None:
        self._set_model()
        source = self._write_plan_md("plan.md", "# Plan\n\n## Phase 1\n\n### Change: `c1`\n\n**Depends on:** None.\n")

        def fake_run(repo, model, prompt):
            return "not valid toml {{{", ""

        original = self.opsx_plan.run_opencode_for_compile
        try:
            self.opsx_plan.run_opencode_for_compile = fake_run
            out = self.repo / "out.toml"
            args = argparse.Namespace(repo=str(self.repo), source="plan.md",
                                      output=str(out), force=False)
            with self.assertRaises(self.opsx_plan.PlanError):
                self.opsx_plan.cmd_compile(args)
            self.assertFalse(out.is_file())
        finally:
            self.opsx_plan.run_opencode_for_compile = original

    def test_cmd_compile_rejects_empty_output(self) -> None:
        self._set_model()
        source = self._write_plan_md("plan.md", "# Plan\n\n## Phase 1\n\n### Change: `c1`\n\n**Depends on:** None.\n")

        def fake_run(repo, model, prompt):
            return "   ", ""

        original = self.opsx_plan.run_opencode_for_compile
        try:
            self.opsx_plan.run_opencode_for_compile = fake_run
            out = self.repo / "out.toml"
            args = argparse.Namespace(repo=str(self.repo), source="plan.md",
                                      output=str(out), force=False)
            with self.assertRaises(self.opsx_plan.PlanError):
                self.opsx_plan.cmd_compile(args)
            self.assertFalse(out.is_file())
        finally:
            self.opsx_plan.run_opencode_for_compile = original

    def test_cmd_compile_rejects_toml_with_no_changes(self) -> None:
        self._set_model()
        source = self._write_plan_md("plan.md", "# Plan\n\n## Phase 1\n\n### Change: `c1`\n\n**Depends on:** None.\n")

        no_changes_toml = '[plan]\nname = "test"\n'

        def fake_run(repo, model, prompt):
            return no_changes_toml, ""

        original = self.opsx_plan.run_opencode_for_compile
        try:
            self.opsx_plan.run_opencode_for_compile = fake_run
            out = self.repo / "out.toml"
            args = argparse.Namespace(repo=str(self.repo), source="plan.md",
                                      output=str(out), force=False)
            with self.assertRaises(self.opsx_plan.PlanError):
                self.opsx_plan.cmd_compile(args)
            self.assertFalse(out.is_file())
        finally:
            self.opsx_plan.run_opencode_for_compile = original

    def test_cmd_compile_rejects_unknown_dependency(self) -> None:
        self._set_model()
        source = self._write_plan_md("plan.md", "# Plan\n\n## Phase 1\n\n### Change: `c1`\n\n**Depends on:** None.\n")

        unknown_dep_toml = (
            '[plan]\nname = "test"\nadapter = "opencode"\n\n'
            "[[changes]]\nid = \"c1\"\nphase = 1\n"
            "depends_on = [\"nonexistent\"]\n"
        )

        def fake_run(repo, model, prompt):
            return unknown_dep_toml, ""

        original = self.opsx_plan.run_opencode_for_compile
        try:
            self.opsx_plan.run_opencode_for_compile = fake_run
            out = self.repo / "out.toml"
            args = argparse.Namespace(repo=str(self.repo), source="plan.md",
                                      output=str(out), force=False)
            with self.assertRaises(self.opsx_plan.PlanError):
                self.opsx_plan.cmd_compile(args)
            self.assertFalse(out.is_file())
        finally:
            self.opsx_plan.run_opencode_for_compile = original

    def test_cmd_compile_rejects_duplicate_change_id(self) -> None:
        self._set_model()
        source = self._write_plan_md("plan.md", "# Plan\n\n## Phase 1\n\n### Change: `c1`\n\n**Depends on:** None.\n")

        dup_id_toml = (
            '[plan]\nname = "test"\nadapter = "opencode"\n\n'
            "[[changes]]\nid = \"c1\"\nphase = 1\n"
            "[[changes]]\nid = \"c1\"\nphase = 2\n"
        )

        def fake_run(repo, model, prompt):
            return dup_id_toml, ""

        original = self.opsx_plan.run_opencode_for_compile
        try:
            self.opsx_plan.run_opencode_for_compile = fake_run
            out = self.repo / "out.toml"
            args = argparse.Namespace(repo=str(self.repo), source="plan.md",
                                      output=str(out), force=False)
            with self.assertRaises(self.opsx_plan.PlanError):
                self.opsx_plan.cmd_compile(args)
            self.assertFalse(out.is_file())
        finally:
            self.opsx_plan.run_opencode_for_compile = original

    def test_cmd_compile_does_not_overwrite_on_failure(self) -> None:
        """Even when --force is passed, an invalid model output must not
        overwrite an existing file."""
        self._set_model()
        source = self._write_plan_md("plan.md", "# Plan\n\n## Phase 1\n\n### Change: `c1`\n\n**Depends on:** None.\n")
        out = self.repo / "out.toml"
        out.write_text("original content", encoding="utf-8")

        def fake_run(repo, model, prompt):
            return "bad toml {{{", ""

        original = self.opsx_plan.run_opencode_for_compile
        try:
            self.opsx_plan.run_opencode_for_compile = fake_run
            args = argparse.Namespace(repo=str(self.repo), source="plan.md",
                                      output=str(out), force=True)
            with self.assertRaises(self.opsx_plan.PlanError):
                self.opsx_plan.cmd_compile(args)
            self.assertEqual(out.read_text(encoding="utf-8"), "original content")
        finally:
            self.opsx_plan.run_opencode_for_compile = original

    # -- extract_toml --

    def test_extract_toml_from_fenced_block(self) -> None:
        output = '```toml\n[plan]\nname = "x"\n```\n'
        result = self.opsx_plan.extract_toml(output)
        self.assertIn('[plan]', result)
        self.assertNotIn('```', result)

    def test_extract_toml_from_bare_output(self) -> None:
        output = '[plan]\nname = "x"\n'
        result = self.opsx_plan.extract_toml(output)
        self.assertEqual(result, output.strip())

    def test_extract_toml_rejects_empty(self) -> None:
        with self.assertRaises(self.opsx_plan.PlanError):
            self.opsx_plan.extract_toml("   ")

    def test_extract_toml_rejects_no_toml(self) -> None:
        with self.assertRaises(self.opsx_plan.PlanError):
            self.opsx_plan.extract_toml("just some prose, no brackets")

    def test_extract_toml_rejects_multiple_fenced_blocks(self) -> None:
        output = (
            '```toml\n[plan]\nname = "x"\n```\n'
            '```toml\n[plan]\nname = "y"\n```\n'
        )
        with self.assertRaises(self.opsx_plan.PlanError) as ctx:
            self.opsx_plan.extract_toml(output)
        self.assertIn("multiple fenced", str(ctx.exception))

    def test_extract_toml_rejects_prose_before_fenced_block(self) -> None:
        output = "Here is the compiled plan:\n\n```toml\n[plan]\nname = \"x\"\n```\n"
        with self.assertRaises(self.opsx_plan.PlanError) as ctx:
            self.opsx_plan.extract_toml(output)
        self.assertIn("extra content found around", str(ctx.exception))

    def test_extract_toml_rejects_prose_after_fenced_block(self) -> None:
        output = "```toml\n[plan]\nname = \"x\"\n```\n\nLet me know if you need changes."
        with self.assertRaises(self.opsx_plan.PlanError) as ctx:
            self.opsx_plan.extract_toml(output)
        self.assertIn("extra content found around", str(ctx.exception))

    def test_extract_toml_accepts_clean_fenced_block_with_surrounding_whitespace(self) -> None:
        output = "\n\n```toml\n[plan]\nname = \"x\"\n```\n\n"
        result = self.opsx_plan.extract_toml(output)
        self.assertIn('[plan]', result)
        self.assertNotIn('```', result)

    # -- CLI parser coverage --

    def test_compile_subcommand_appears_in_help(self) -> None:
        """Prove ``compile`` appears in the subcommand list."""
        self.opsx_plan.sys = mock.Mock()
        stderr = io.StringIO()

        with mock.patch.object(self.opsx_plan.sys, "argv", ["opsx-plan", "--help"]), \
             mock.patch("sys.stdout", io.StringIO()) as stdout, \
             mock.patch("sys.stderr", stderr):
            # argparse calls sys.exit on --help; suppress it
            try:
                self.opsx_plan.main()
            except SystemExit:
                pass

        combined = stdout.getvalue() + stderr.getvalue()
        self.assertIn("compile", combined)

    def test_compile_subcommand_routes_to_cmd_compile(self) -> None:
        """Prove ``opsx-plan compile`` routes to ``cmd_compile``."""
        self._set_model()
        source = self._write_plan_md("plan.md", "# Plan\n\n## Phase 1\n\n### Change: `c1`\n\n**Depends on:** None.\n")
        out = self.repo / "out.toml"

        valid_toml = (
            '[plan]\nname = "test"\nadapter = "opencode"\n\n'
            "[[changes]]\nid = \"c1\"\nphase = 1\n"
        )

        def fake_run(repo, model, prompt):
            return valid_toml, ""

        original = self.opsx_plan.run_opencode_for_compile
        try:
            self.opsx_plan.run_opencode_for_compile = fake_run
            with mock.patch.object(
                self.opsx_plan.sys,
                "argv",
                ["opsx-plan", "--repo", str(self.repo),
                 "compile", "plan.md", "-o", str(out)],
            ):
                rc = self.opsx_plan.main()
            self.assertEqual(rc, 0)
            self.assertTrue(out.is_file())
        finally:
            self.opsx_plan.run_opencode_for_compile = original

    def test_run_opencode_for_compile_raises_on_spawn_failure(self) -> None:
        def fake_run(*args, **kwargs):
            raise FileNotFoundError("no opencode")

        with mock.patch("subprocess.run", side_effect=fake_run):
            with self.assertRaises(self.opsx_plan.PlanError) as ctx:
                self.opsx_plan.run_opencode_for_compile(self.repo, "m", "prompt")
            self.assertIn("could not spawn opencode", str(ctx.exception))

    def test_run_opencode_for_compile_passes_model_in_argv(self) -> None:
        """Verify ``run_opencode_for_compile`` spawns opencode with the
        configured model."""
        model = "configured-model-v1"
        prompt = "compile this plan"

        real_run = subprocess.run

        def fake_run(args, **kwargs):
            self.assertEqual(args[0], "opencode")
            self.assertEqual(args[1], "run")
            self.assertEqual(args[2], "--model")
            self.assertEqual(args[3], model)
            self.assertEqual(args[4], prompt)
            result = mock.Mock()
            result.returncode = 0
            result.stdout = '[plan]\nname = "x"\n\n[[changes]]\nid = "c1"\n'
            result.stderr = ""
            return result

        with mock.patch("subprocess.run", side_effect=fake_run):
            stdout, stderr = self.opsx_plan.run_opencode_for_compile(
                self.repo, model, prompt
            )

    def test_run_opencode_for_compile_raises_on_nonzero_exit(self) -> None:
        fake_result = mock.Mock()
        fake_result.returncode = 1
        fake_result.stdout = ""
        fake_result.stderr = "some error"

        with mock.patch("subprocess.run", return_value=fake_result):
            with self.assertRaises(self.opsx_plan.PlanError) as ctx:
                self.opsx_plan.run_opencode_for_compile(self.repo, "m", "prompt")
            self.assertIn("exited with code 1", str(ctx.exception))

    def test_build_schema_guidance_includes_load_plan_fields(self) -> None:
        guidance = self.opsx_plan.build_schema_guidance()
        for field in ("name", "adapter", "invoke", "implement_invoke",
                       "review_invoke", "archive_invoke", "timeout_minutes",
                       "max_rounds", "no_progress_limit", "fast_checks",
                       "plan_doc", "create_invoke", "pause_before", "depends_on",
                       "enabled", "phase", "id", "max_attempts", "review_created"):
            self.assertIn(field, guidance,
                          f"schema guidance must mention field '{field}' consumed by load_plan()")


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
        usage, model = self.opsx_plan.extract_usage_and_model(payload, None)
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
        usage, _ = self.opsx_plan.extract_usage_and_model(payload, None)
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
        usage, _ = self.opsx_plan.extract_usage_and_model(payload, None)
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
        usage, _ = self.opsx_plan.extract_usage_and_model(payload, None)
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
        _, model = self.opsx_plan.extract_usage_and_model(payload, None)
        self.assertEqual(model["provider"], "openai")
        self.assertEqual(model["model_id"], "gpt-5.5")
        self.assertEqual(model["model_alias"], "primary")

    def test_worker_json_model_top_level_alternate_keys(self) -> None:
        payload = {
            "status": "implemented",
            "change": "ex",
            "provider": "anthropic",
            "modelId": "claude-4",
        }
        _, model = self.opsx_plan.extract_usage_and_model(payload, None)
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
        usage, _ = self.opsx_plan.extract_usage_and_model(payload, log_path)
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
        _, model = self.opsx_plan.extract_usage_and_model(payload, log_path)
        self.assertEqual(model["provider"], "openai")
        self.assertEqual(model["model_id"], "gpt-5.5")

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
        usage, _ = self.opsx_plan.extract_usage_and_model(payload, log_path)
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
        _, model = self.opsx_plan.extract_usage_and_model(payload, log_path)
        self.assertEqual(model["provider"], "worker-provider")
        self.assertEqual(model["model_id"], "worker-model")

    def test_log_model_fallback_blocked_when_worker_has_any_model_field(self) -> None:
        """Worker provides provider only; log model fallback is blocked because worker already carries a model field."""
        log_path = self._write_log(
            '{"model_id": "log-model-id"}',
        )
        payload = {"status": "implemented", "change": "ex", "provider": "openai"}
        _, model = self.opsx_plan.extract_usage_and_model(payload, log_path)
        self.assertEqual(model["provider"], "openai")
        self.assertIsNone(model["model_id"])

    def test_log_model_fallback_when_worker_has_no_model(self) -> None:
        """Log provides model identity when worker JSON has none."""
        log_path = self._write_log(
            '{"model_id": "log-model-id", "provider": "log-provider"}',
        )
        payload = {"status": "implemented", "change": "ex"}
        _, model = self.opsx_plan.extract_usage_and_model(payload, log_path)
        self.assertEqual(model["provider"], "log-provider")
        self.assertEqual(model["model_id"], "log-model-id")

    # -- 4.7 Unknown formats -----------------------------------------------

    def test_unknown_format_produces_unavailable_usage(self) -> None:
        payload = {"status": "implemented", "change": "ex"}
        usage, model = self.opsx_plan.extract_usage_and_model(payload, None)
        self._assert_usage_unavailable(usage)
        self._assert_model_null(model)

    def test_log_without_usage_produces_unavailable(self) -> None:
        log_path = self._write_log(
            "# just some text",
            "no json here",
            '{"some_other_field": 42}',
        )
        payload = {"status": "implemented", "change": "ex"}
        usage, _ = self.opsx_plan.extract_usage_and_model(payload, log_path)
        self._assert_usage_unavailable(usage)

    def test_none_payload_produces_unavailable(self) -> None:
        usage, model = self.opsx_plan.extract_usage_and_model(None, None)
        self._assert_usage_unavailable(usage)
        self._assert_model_null(model)

    # -- 4.8 Malformed usage values ----------------------------------------

    def test_negative_token_value_ignored(self) -> None:
        payload = {
            "status": "implemented",
            "change": "ex",
            "usage": {"input_tokens": -1, "output_tokens": 20},
        }
        usage, _ = self.opsx_plan.extract_usage_and_model(payload, None)
        self.assertTrue(usage["usage_available"])
        self.assertIsNone(usage["input_tokens"])
        self.assertEqual(usage["output_tokens"], 20)

    def test_floating_point_token_value_ignored(self) -> None:
        payload = {
            "status": "implemented",
            "change": "ex",
            "usage": {"input_tokens": 100.5},
        }
        usage, _ = self.opsx_plan.extract_usage_and_model(payload, None)
        self._assert_usage_unavailable(usage)

    def test_non_numeric_token_value_ignored(self) -> None:
        payload = {
            "status": "implemented",
            "change": "ex",
            "usage": {"input_tokens": "100", "total_tokens": None},
        }
        usage, _ = self.opsx_plan.extract_usage_and_model(payload, None)
        self._assert_usage_unavailable(usage)

    def test_boolean_token_value_ignored(self) -> None:
        payload = {
            "status": "implemented",
            "change": "ex",
            "usage": {"input_tokens": True, "output_tokens": False},
        }
        usage, _ = self.opsx_plan.extract_usage_and_model(payload, None)
        self._assert_usage_unavailable(usage)

    # -- 4.9 Default-unavailable for failure outcomes -----------------------

    def test_timeout_record_usage_default_unavailable(self) -> None:
        """Simulate payload=None (timeout path) keeps usage unavailable."""
        usage, model = self.opsx_plan.extract_usage_and_model(None, None)
        self._assert_usage_unavailable(usage)
        self._assert_model_null(model)

    def test_spawn_error_record_usage_default_unavailable(self) -> None:
        usage, model = self.opsx_plan.extract_usage_and_model(None, None)
        self._assert_usage_unavailable(usage)
        self._assert_model_null(model)

    def test_invalid_output_record_usage_default_unavailable(self) -> None:
        usage, model = self.opsx_plan.extract_usage_and_model(None, None)
        self._assert_usage_unavailable(usage)
        self._assert_model_null(model)

    # -- Edge cases ---------------------------------------------------------

    def test_extraction_never_raises_on_broken_payload(self) -> None:
        """Extraction must be best-effort and never raise."""
        # A payload that is a dict but has weird internal types should not
        # crash the extractor.
        payload = {"usage": "not_a_dict"}
        usage, model = self.opsx_plan.extract_usage_and_model(payload, None)
        self._assert_usage_unavailable(usage)
        self._assert_model_null(model)

    def test_log_with_malformed_json_lines_is_ignored(self) -> None:
        log_path = self._write_log(
            "{not valid json",
            '{"input_tokens": 50}',
            "{still not valid",
        )
        payload = {"status": "implemented", "change": "ex"}
        usage, _ = self.opsx_plan.extract_usage_and_model(payload, log_path)
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
        usage, _ = self.opsx_plan.extract_usage_and_model(payload, None)
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
        _, model = self.opsx_plan.extract_usage_and_model(payload, None)
        self.assertEqual(model["provider"], "openai")
        self.assertEqual(model["model_id"], "gpt-4")


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
        record = self.opsx_plan.rec(self.state, self.cid)
        record["max_rounds"] = self.cfg["max_rounds"]
        record["tracked_change_files"] = self.opsx_plan.change_context_paths(
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
                "completed_tasks": ["1.1"],
                "remaining_tasks": ["1.2"],
                "task_counts": {"complete": 1, "total": 2},
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
        record = self.opsx_plan.rec(self.state, self.cid)
        record["max_rounds"] = self.cfg["max_rounds"]
        record["tracked_change_files"] = self.opsx_plan.change_context_paths(
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
        record = self.opsx_plan.rec(self.state, self.cid)
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
        record = self.opsx_plan.rec(self.state, self.cid)
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
        record = self.opsx_plan.rec(self.state, self.cid)
        record["max_rounds"] = self.cfg["max_rounds"]
        record["tracked_change_files"] = self.opsx_plan.change_context_paths(
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

    # -- Telemetry write resilience -----------------------------------------

    def test_extraction_failure_does_not_block_telemetry_write(self) -> None:
        """Verify that even if extraction raises, the telemetry record is
        still written with default-unavailable usage."""
        self.write_authored_change(self.cid)
        record = self.opsx_plan.rec(self.state, self.cid)
        record["max_rounds"] = self.cfg["max_rounds"]
        record["tracked_change_files"] = self.opsx_plan.change_context_paths(
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
            self.opsx_plan, "extract_usage_and_model", side_effect=bad_extraction
        ):
            self.opsx_plan.run_direct_change(self.repo, self.cfg, self.state, self.cid)

        records = self._read_telemetry()
        self.assertGreaterEqual(len(records), 1)
        u = records[0]["usage"]
        # Must have default-unavailable (the try/except caught the error)
        self.assertFalse(u["usage_available"])
        self.assertIsNone(u["usage_source"])


class CostEstimationTests(unittest.TestCase):
    """Unit tests for cost estimation functions (tasks 5.1-5.8)."""

    def setUp(self) -> None:
        self.opsx_plan = load_opsx_plan()
        # Reset module-level catalog so tests can use their own.
        self.opsx_plan._cost_catalog = None
        self._saved_denoms = dict(self.opsx_plan.SUBSCRIPTION_DENOMINATORS)
        self.opsx_plan.SUBSCRIPTION_DENOMINATORS.clear()

    def tearDown(self) -> None:
        self.opsx_plan._cost_catalog = None
        self.opsx_plan.SUBSCRIPTION_DENOMINATORS.clear()
        self.opsx_plan.SUBSCRIPTION_DENOMINATORS.update(self._saved_denoms)

    @staticmethod
    def _write_catalog(content: str) -> Path:
        """Write a temporary TOML catalog and return its path."""
        from textwrap import dedent

        tmp = tempfile.NamedTemporaryFile(
            mode="w", suffix=".toml", delete=False, encoding="utf-8",
        )
        tmp.write(dedent(content))
        tmp.close()
        return Path(tmp.name)

    def _set_catalog(self, content: str) -> None:
        """Replace the module-level catalog with one built from *content*."""
        from lib.pricing import PricingCatalog, UnresolvedPrice

        catalog_path = self._write_catalog(content)
        self.opsx_plan._cost_catalog = (PricingCatalog(catalog_path=catalog_path), UnresolvedPrice)

    def _usage(self, **kwargs):
        """Build a usage dict with defaults for a typical available scenario."""
        defaults = {
            "usage_available": True,
            "input_tokens": None,
            "output_tokens": None,
            "cached_input_tokens": None,
            "reasoning_tokens": None,
            "total_tokens": None,
        }
        defaults.update(kwargs)
        return defaults

    def _model(self, provider="openai", model_id="gpt-4o"):
        return {"provider": provider, "model_id": model_id}

    # 5.1
    def test_per_token_model_with_input_output_produces_estimated(self):
        self._set_catalog(
            """\
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
            """
        )
        usage = self._usage(input_tokens=200000, output_tokens=50000)
        model = self._model()

        result = self.opsx_plan.estimate_stage_cost(usage, model)

        self.assertEqual(result["status"], "estimated")
        self.assertEqual(result["pricing_catalog_version"], "1.0.0")
        self.assertEqual(result["estimated_cost"], 0.8)
        self.assertIsNone(result["unresolved_reason"])
        snapshot = result["price_snapshot"]
        self.assertIsNotNone(snapshot)
        self.assertEqual(snapshot["provider"], "openai")
        self.assertEqual(snapshot["model_id"], "gpt-4o")
        self.assertEqual(snapshot["billing_mode"], "per_token")
        self.assertEqual(snapshot["input_price_per_mtok"], 2.0)
        self.assertEqual(snapshot["output_price_per_mtok"], 8.0)

    # 5.2
    def test_cached_input_tokens_contribute_to_estimate(self):
        self._set_catalog(
            """\
            [catalog]
            version = "1.0.0"
            updated = "2026-01-01"

            [[entries]]
            provider = "openai"
            model_id = "gpt-4o"
            display_name = "GPT-4o"
            billing_mode = "per_token"
            currency = "USD"
            input_price_per_mtok = 2.50
            output_price_per_mtok = 10.00
            cached_input_price_per_mtok = 1.25
            effective_date = "2025-01-01"
            """
        )
        # 100000 cached input tokens at $1.25/mtok = $0.125
        usage = self._usage(cached_input_tokens=100000)
        model = self._model()

        result = self.opsx_plan.estimate_stage_cost(usage, model)

        self.assertEqual(result["status"], "estimated")
        self.assertEqual(result["estimated_cost"], 0.125)
        snapshot = result["price_snapshot"]
        self.assertEqual(snapshot["cached_input_price_per_mtok"], 1.25)

    # 5.3
    def test_usage_unavailable_produces_unresolved(self):
        usage = self._usage(usage_available=False)
        model = self._model()

        result = self.opsx_plan.estimate_stage_cost(usage, model)

        self.assertEqual(result["status"], "unresolved")
        self.assertEqual(result["unresolved_reason"], "usage unavailable")
        self.assertIsNone(result["estimated_cost"])
        self.assertIsNone(result["price_snapshot"])

    # 5.4
    def test_missing_model_identity_produces_unresolved(self):
        self._set_catalog(
            """\
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
            effective_date = "2025-01-01"
            """
        )
        usage = self._usage(input_tokens=100)

        for model in (
            {"provider": "", "model_id": ""},
            {"provider": None, "model_id": None},
            {"provider": "openai", "model_id": ""},
            {"provider": "", "model_id": "gpt-4o"},
        ):
            with self.subTest(model=model):
                result = self.opsx_plan.estimate_stage_cost(usage, model)
                self.assertEqual(result["status"], "unresolved")
                self.assertEqual(
                    result["unresolved_reason"], "model identity unavailable",
                )

    # 5.5
    def test_unknown_model_pricing_produces_unresolved(self):
        self._set_catalog(
            """\
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
            effective_date = "2025-01-01"
            """
        )
        usage = self._usage(input_tokens=100)

        # Unknown model
        result = self.opsx_plan.estimate_stage_cost(
            usage, {"provider": "openai", "model_id": "gpt-99"},
        )
        self.assertEqual(result["status"], "unresolved")
        self.assertIn("unknown model", result["unresolved_reason"])

        # Unknown provider
        result = self.opsx_plan.estimate_stage_cost(
            usage, {"provider": "nobody", "model_id": "model"},
        )
        self.assertEqual(result["status"], "unresolved")
        self.assertIn("unknown provider", result["unresolved_reason"])

    # 5.6
    def test_token_category_positive_usage_unpriced_produces_unresolved(self):
        self._set_catalog(
            """\
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
            effective_date = "2025-01-01"
            """
        )
        # reasoning_tokens has positive usage but no reasoning_rate in catalog
        usage = self._usage(reasoning_tokens=1000)
        model = self._model()

        result = self.opsx_plan.estimate_stage_cost(usage, model)

        self.assertEqual(result["status"], "unresolved")
        self.assertIn("missing rate for observed token category", result["unresolved_reason"])
        self.assertIn("reasoning_tokens", result["unresolved_reason"])

    # 5.7
    def test_subscription_with_valid_denominator_produces_estimated(self):
        self._set_catalog(
            """\
            [catalog]
            version = "1.0.0"
            updated = "2026-01-01"
            [[entries]]
            provider = "github"
            model_id = "copilot"
            display_name = "GitHub Copilot"
            billing_mode = "subscription"
            currency = "USD"
            subscription_period = "monthly"
            subscription_price = 10.0
            effective_date = "2025-01-01"
            """
        )
        # usage.total_tokens = 100000, denominator = 50000000
        # expected cost = 10.0 * (100000 / 50000000) = 0.02
        usage = self._usage(total_tokens=100000)
        model = self._model("github", "copilot")
        denoms = {"github": {"copilot": 50000000.0}}

        result = self.opsx_plan.estimate_stage_cost(usage, model, denoms)

        self.assertEqual(result["status"], "estimated")
        self.assertEqual(result["estimated_cost"], 0.02)
        snapshot = result["price_snapshot"]
        self.assertIsNotNone(snapshot)
        self.assertEqual(snapshot["billing_mode"], "subscription")
        self.assertEqual(snapshot["subscription_price"], 10.0)
        self.assertEqual(snapshot["usage_denominator_units"], 50000000)
        self.assertEqual(snapshot["usage_denominator_source"], "config")

    # 5.8
    def test_subscription_without_denominator_produces_unresolved(self):
        self._set_catalog(
            """\
            [catalog]
            version = "1.0.0"
            updated = "2026-01-01"
            [[entries]]
            provider = "github"
            model_id = "copilot"
            display_name = "GitHub Copilot"
            billing_mode = "subscription"
            currency = "USD"
            subscription_period = "monthly"
            subscription_price = 10.0
            effective_date = "2025-01-01"
            """
        )
        usage = self._usage(total_tokens=100000)
        model = self._model("github", "copilot")

        result = self.opsx_plan.estimate_stage_cost(usage, model)

        self.assertEqual(result["status"], "unresolved")
        self.assertEqual(
            result["unresolved_reason"], "missing subscription denominator",
        )
        self.assertIsNone(result["estimated_cost"])

    def test_subscription_uses_total_tokens_from_categories_when_total_is_null(self):
        self._set_catalog(
            """\
            [catalog]
            version = "1.0.0"
            updated = "2026-01-01"
            [[entries]]
            provider = "github"
            model_id = "copilot"
            display_name = "GitHub Copilot"
            billing_mode = "subscription"
            currency = "USD"
            subscription_period = "monthly"
            subscription_price = 10.0
            effective_date = "2025-01-01"
            """
        )
        # total_tokens is None, but input + output = 200000
        usage = self._usage(total_tokens=None, input_tokens=150000, output_tokens=50000)
        model = self._model("github", "copilot")
        denoms = {"github": {"copilot": 50000000.0}}

        result = self.opsx_plan.estimate_stage_cost(usage, model, denoms)

        self.assertEqual(result["status"], "estimated")
        # 10.0 * (200000 / 50000000) = 0.04
        self.assertEqual(result["estimated_cost"], 0.04)

    def test_module_level_denominators_are_used(self):
        self._set_catalog(
            """\
            [catalog]
            version = "1.0.0"
            updated = "2026-01-01"
            [[entries]]
            provider = "github"
            model_id = "copilot"
            display_name = "GitHub Copilot"
            billing_mode = "subscription"
            currency = "USD"
            subscription_period = "monthly"
            subscription_price = 10.0
            effective_date = "2025-01-01"
            """
        )
        usage = self._usage(total_tokens=100000)
        model = self._model("github", "copilot")
        self.opsx_plan.SUBSCRIPTION_DENOMINATORS["github"] = {"copilot": 50000000.0}

        result = self.opsx_plan.estimate_stage_cost(usage, model)

        self.assertEqual(result["status"], "estimated")
        self.assertEqual(result["estimated_cost"], 0.02)

    def test_invalid_denominator_produces_unresolved(self):
        self._set_catalog(
            """\
            [catalog]
            version = "1.0.0"
            updated = "2026-01-01"
            [[entries]]
            provider = "github"
            model_id = "copilot"
            display_name = "GitHub Copilot"
            billing_mode = "subscription"
            currency = "USD"
            subscription_period = "monthly"
            subscription_price = 10.0
            effective_date = "2025-01-01"
            """
        )
        usage = self._usage(total_tokens=100000)
        model = self._model("github", "copilot")

        for bad_denom in (-1, 0):
            with self.subTest(denom=bad_denom):
                denoms = {"github": {"copilot": float(bad_denom)}}
                result = self.opsx_plan.estimate_stage_cost(usage, model, denoms)
                self.assertEqual(result["status"], "unresolved")
                self.assertEqual(
                    result["unresolved_reason"], "invalid subscription denominator",
                )

    def test_non_numeric_and_nan_denominator_produce_unresolved(self):
        """Regression: non-numeric and NaN denominator values must produce
        unresolved status instead of crashing or producing NaN costs."""
        self._set_catalog(
            """\
            [catalog]
            version = "1.0.0"
            updated = "2026-01-01"
            [[entries]]
            provider = "github"
            model_id = "copilot"
            display_name = "GitHub Copilot"
            billing_mode = "subscription"
            currency = "USD"
            subscription_period = "monthly"
            subscription_price = 10.0
            effective_date = "2025-01-01"
            """
        )
        usage = self._usage(total_tokens=100000)
        model = self._model("github", "copilot")

        # Non-numeric string values should not crash or fall back to
        # "unavailable" — they should produce "unresolved" with a clear
        # reason.
        for bad_denom in ("not-a-number", "invalid", ""):
            with self.subTest(denom=bad_denom):
                denoms = {"github": {"copilot": bad_denom}}
                result = self.opsx_plan.estimate_stage_cost(usage, model, denoms)
                self.assertEqual(result["status"], "unresolved")
                self.assertEqual(
                    result["unresolved_reason"], "invalid subscription denominator",
                )

        # NaN values (float('nan')) must also produce "unresolved" — they are
        # not usable numbers even though isinstance checks pass.
        denoms = {"github": {"copilot": float("nan")}}
        result = self.opsx_plan.estimate_stage_cost(usage, model, denoms)
        self.assertEqual(result["status"], "unresolved")
        self.assertEqual(
            result["unresolved_reason"], "invalid subscription denominator",
        )

    def test_zero_token_count_contributes_zero_cost(self):
        self._set_catalog(
            """\
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
            """
        )
        usage = self._usage(input_tokens=0, output_tokens=0)
        model = self._model()

        result = self.opsx_plan.estimate_stage_cost(usage, model)

        self.assertEqual(result["status"], "estimated")
        self.assertEqual(result["estimated_cost"], 0.0)

    def test_null_token_count_does_not_contribute(self):
        self._set_catalog(
            """\
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
            """
        )
        # Only input tokens present; output is null
        usage = self._usage(input_tokens=100000, output_tokens=None)
        model = self._model()

        result = self.opsx_plan.estimate_stage_cost(usage, model)

        self.assertEqual(result["status"], "estimated")
        self.assertEqual(result["estimated_cost"], 0.2)  # 100000/1e6 * 2.0

    def test_price_snapshot_per_token_includes_all_rates(self):
        self._set_catalog(
            """\
            [catalog]
            version = "1.2.3"
            updated = "2026-01-01"
            [[entries]]
            provider = "openai"
            model_id = "o3"
            display_name = "o3"
            billing_mode = "per_token"
            currency = "USD"
            input_price_per_mtok = 10.0
            output_price_per_mtok = 40.0
            cached_input_price_per_mtok = 2.5
            reasoning_price_per_mtok = 20.0
            effective_date = "2025-01-01"
            """
        )
        usage = self._usage(input_tokens=10000, output_tokens=5000,
                            cached_input_tokens=2000, reasoning_tokens=1000)
        model = self._model("openai", "o3")

        result = self.opsx_plan.estimate_stage_cost(usage, model)

        self.assertEqual(result["status"], "estimated")
        snapshot = result["price_snapshot"]
        self.assertEqual(snapshot["catalog_version"], "1.2.3")
        self.assertEqual(snapshot["input_price_per_mtok"], 10.0)
        self.assertEqual(snapshot["output_price_per_mtok"], 40.0)
        self.assertEqual(snapshot["cached_input_price_per_mtok"], 2.5)
        self.assertEqual(snapshot["reasoning_price_per_mtok"], 20.0)
        self.assertEqual(snapshot["display_name"], "o3")
        self.assertNotIn("subscription_price", snapshot)

    def test_empty_catalog_produces_unresolved(self):
        self._set_catalog(
            """\
            [catalog]
            version = "1.0.0"
            updated = "2026-01-01"
            """
        )
        usage = self._usage(input_tokens=100)
        model = self._model()

        result = self.opsx_plan.estimate_stage_cost(usage, model)

        self.assertEqual(result["status"], "unresolved")
        self.assertEqual(result["unresolved_reason"], "empty catalog")

    def test_cost_estimation_does_not_crash_on_uninitialized_catalog(self):
        """When the catalog fails to load, estimation returns unresolved."""
        self.opsx_plan._cost_catalog = False  # sentinel for failed init
        usage = self._usage(input_tokens=100)
        model = self._model()

        result = self.opsx_plan.estimate_stage_cost(usage, model)

        self.assertEqual(result["status"], "unresolved")
        self.assertEqual(
            result["unresolved_reason"], "pricing catalog failed to load",
        )

    def test_cli_path_regression_repo_arg_loads_real_catalog(self):
        """CLI-path regression: when repo is passed, the installed
        opsx-plan/opsx-run can discover lib.pricing and load the real
        catalog for cost estimation."""
        import sys

        self.opsx_plan._cost_catalog = None
        actual_repo = Path(__file__).resolve().parents[2]
        usage = self._usage(input_tokens=100000, output_tokens=50000)
        model = self._model("openai", "gpt-4o")

        # Cold-start lib.pricing: earlier tests may have imported
        # lib.pricing (e.g. via _set_catalog), which caches it in
        # sys.modules and masks the CLI-path regression.  Pop every
        # key rooted at 'lib' so the next import is a true cold load.
        saved_lib_modules = {
            k: v for k, v in sys.modules.items()
            if k == "lib" or k.startswith("lib.")
        }
        for k in saved_lib_modules:
            del sys.modules[k]

        # Temporarily remove the repo root from sys.path to simulate
        # the installed CLI environment where only the script directory
        # is on the path.
        repo_str = str(actual_repo)
        removed: list[str] = []
        while repo_str in sys.path:
            sys.path.remove(repo_str)
            removed.append(repo_str)

        try:
            result = self.opsx_plan.estimate_stage_cost(usage, model,
                                                         repo=actual_repo)
        finally:
            # Restore whatever we removed.
            for p in removed:
                if p not in sys.path:
                    sys.path.insert(0, p)
            # Restore saved lib modules so later tests are not affected.
            for k, v in saved_lib_modules.items():
                sys.modules[k] = v

        self.assertEqual(result["status"], "estimated",
                         f"expected estimated, got {result}")
        self.assertEqual(result["pricing_catalog_version"], "1.0.0")
        # input 100k * 2.50/mtok + output 50k * 10.00/mtok
        # = 0.25 + 0.50 = 0.75
        self.assertEqual(result["estimated_cost"], 0.75)
        self.assertIsNotNone(result["price_snapshot"])


if __name__ == "__main__":
    unittest.main()
