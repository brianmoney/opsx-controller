from __future__ import annotations

import argparse
import importlib.util
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

SCRIPT = Path(__file__).resolve().parents[2] / "orchestrator" / "opsx-plan.py"

# Pre-compiled regex for extracting the fenced TOML block emitted by
# build_schema_guidance.
_TOM_BLOCK = re.compile(r"```toml\s*\n(.*?)```", re.DOTALL)

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


def load_opsx_plan():
    spec = importlib.util.spec_from_file_location("opsx_plan", SCRIPT)
    assert spec is not None
    assert spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def git(repo: Path, *args: str) -> None:
    subprocess.run(["git", *args], cwd=repo, check=True, capture_output=True, text=True)


class DirectOpenCodeExecutionTests(unittest.TestCase):
    def setUp(self) -> None:
        self.opsx_plan = load_opsx_plan()
        self.tmp = tempfile.TemporaryDirectory()
        self.repo = Path(self.tmp.name)
        git(self.repo, "init")
        (self.repo / "tracked.txt").write_text("base\n", encoding="utf-8")
        # Mirror this repo's own layout: openspec/changes/archive/ is
        # gitignored, so archiving stages nothing and produces no
        # archive(<id>): commit. Tests that assert the commit is optional
        # depend on this actually being ignored, not just assumed.
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
        self.cid = "add-example"
        self.plan_name = "direct-plan"
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
        self.write_authored_change(self.cid)
        record = self.opsx_plan.state_mod.rec(self.state, self.cid)
        record["max_rounds"] = self.cfg["max_rounds"]
        record["tracked_change_files"] = self.opsx_plan.state_mod.change_context_paths(
            self.repo, self.cid
        )
        self._saved_invoke = self.opsx_plan.invoke_direct_stage
        self._saved_checks = self.opsx_plan.groundtruth.run_fast_checks

    def tearDown(self) -> None:
        self.opsx_plan.invoke_direct_stage = self._saved_invoke
        self.opsx_plan.groundtruth.run_fast_checks = self._saved_checks
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

        self.assertEqual(result, self.opsx_plan.base.DONE)
        self.assertEqual([stage for stage, _, _ in calls], ["implement", "review", "archive"])
        self.assertIn(f"CHANGE: {self.cid}", inputs[0])
        self.assertIn("ROUND: 1", inputs[0])

        record = self.opsx_plan.state_mod.rec(self.state, self.cid)
        self.assertEqual(record["phase"], "done")
        self.assertEqual(record["status"], self.opsx_plan.base.DONE)
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
        # Default invalid_output_retries=2: the stage is attempted three
        # times (initial + two retries) before the change fails.
        calls, inputs = self.stage_runner(
            [
                {
                    "stage": "implement",
                    "lines": "not json\nsecond line\n",
                },
                {
                    "stage": "implement",
                    "lines": "still not json\n",
                },
                {
                    "stage": "implement",
                    "lines": "prose again\n",
                },
            ]
        )

        result = self.opsx_plan.run_direct_change(self.repo, self.cfg, self.state, self.cid)

        self.assertEqual(result, "failed")
        self.assertEqual(len(calls), 3)
        # Retry attempts carry the corrective hint; the first does not.
        self.assertNotIn("RETRY_CORRECTION", inputs[0])
        self.assertIn("RETRY_CORRECTION", inputs[1])
        self.assertIn("RETRY_CORRECTION", inputs[2])
        record = self.opsx_plan.state_mod.rec(self.state, self.cid)
        self.assertEqual(record["last_result"], "subagent_output_invalid")
        self.assertEqual(record["status"], self.opsx_plan.base.FAILED)
        self.assertIn("output invalid", record["reason"])

    def test_invalid_output_retry_recovers_and_continues(self) -> None:
        valid_implement = {
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
            "cache_update": {
                "change_summary": "direct execution change summary",
                "refresh_reason": "initial direct round",
                "source_paths": [f"openspec/changes/{self.cid}/tasks.md"],
                "scope_hint": "opsx-plan direct orchestration",
            },
        }
        calls, inputs = self.stage_runner(
            [
                {"stage": "implement", "lines": "a prose summary, no JSON\n"},
                {"stage": "implement", "result": valid_implement},
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

        self.assertEqual(result, self.opsx_plan.base.DONE)
        self.assertEqual([stage for stage, _, _ in calls], ["implement", "implement", "review", "archive"])
        self.assertNotIn("RETRY_CORRECTION", inputs[0])
        self.assertIn("RETRY_CORRECTION", inputs[1])
        record = self.opsx_plan.state_mod.rec(self.state, self.cid)
        self.assertEqual(record["status"], self.opsx_plan.base.DONE)

    def test_invalid_output_retries_zero_disables_retry(self) -> None:
        self.cfg["invalid_output_retries"] = 0
        calls, _ = self.stage_runner(
            [
                {
                    "stage": "implement",
                    "lines": "not json\n",
                },
            ]
        )

        result = self.opsx_plan.run_direct_change(self.repo, self.cfg, self.state, self.cid)

        self.assertEqual(result, "failed")
        self.assertEqual(len(calls), 1)
        record = self.opsx_plan.state_mod.rec(self.state, self.cid)
        self.assertEqual(record["last_result"], "subagent_output_invalid")

    def test_permission_rejection_is_not_retried(self) -> None:
        calls, _ = self.stage_runner(
            [
                {
                    "stage": "implement",
                    "lines": "The user rejected permission for tool X\n",
                },
            ]
        )

        result = self.opsx_plan.run_direct_change(self.repo, self.cfg, self.state, self.cid)

        self.assertEqual(result, "failed")
        self.assertEqual(len(calls), 1)
        record = self.opsx_plan.state_mod.rec(self.state, self.cid)
        self.assertEqual(record["last_result"], "subagent_output_invalid")
        self.assertIn("permission denied", record["reason"])

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

        self.assertEqual(result, self.opsx_plan.base.DONE)
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
        record = self.opsx_plan.state_mod.rec(self.state, self.cid)
        self.assertEqual(record["status"], self.opsx_plan.base.FAILED)
        self.assertEqual(record["archive"]["status"], "failed")
        self.assertEqual(record["last_result"], "post_archive_dirty_tracked")
        self.assertIn("post-archive tracked worktree is dirty", record["reason"])

    def test_archive_succeeds_with_no_archive_commit_when_nothing_staged(self) -> None:
        """openspec/changes/archive/ is gitignored: when the change directory
        is untracked and nothing else is in scope, the archiver has nothing
        to commit. The archive commit is corroborating, not required -- the
        change should still reach DONE from the on-disk move alone."""
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
                    "files_touched": [],
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
                src = repo / "openspec" / "changes" / cid
                archive_rel = f"openspec/changes/archive/2026-07-02-{cid}"
                dst = repo / archive_rel
                dst.parent.mkdir(parents=True, exist_ok=True)
                src.rename(dst)
                # The change directory was never git-tracked in this test
                # repo and the archive destination is gitignored, so there
                # is nothing to stage or commit.
                payload = {
                    "status": "archived",
                    "change": cid,
                    "archive_path": archive_rel,
                    "spec_sync_status": "no-delta",
                    "commit": "",
                    "summary": "archive succeeded",
                }
            log_path.write_text(json.dumps(payload) + "\n", encoding="utf-8")
            return "exited", log_path

        self.opsx_plan.invoke_direct_stage = fake_invoke

        result = self.opsx_plan.run_direct_change(self.repo, self.cfg, self.state, self.cid)

        self.assertEqual(result, self.opsx_plan.base.DONE)
        record = self.opsx_plan.state_mod.rec(self.state, self.cid)
        self.assertEqual(record["status"], self.opsx_plan.base.DONE)
        self.assertEqual(record["archive"]["status"], "passed")
        self.assertEqual(record["archive"]["commit"], "")

        ok, why = self.opsx_plan.verify_direct_archive_done(self.repo, self.cid, record)
        self.assertTrue(ok, why)

        # reconcile() must also resolve this from disk alone on a fresh
        # in-memory state, without relying on the archive commit.
        fresh_state = {"plan": self.plan_name, "approvals": [], "changes": {}}
        self.opsx_plan.reconcile(self.repo, self.cfg, fresh_state)
        fresh_record = self.opsx_plan.state_mod.rec(fresh_state, self.cid)
        self.assertEqual(fresh_record["status"], self.opsx_plan.base.DONE)

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
        self.assertEqual(result, self.opsx_plan.base.DONE)

        archived_tasks = (
            self.repo
            / "openspec"
            / "changes"
            / "archive"
            / f"2026-07-02-{self.cid}"
            / "tasks.md"
        )
        archived_tasks.write_text("## 1. Tasks\n\n- [x] 1.1 Example task\n", encoding="utf-8")
        # -f: the archive directory is gitignored in this fixture, and this
        # test needs a real archive(<id>): commit to exercise the
        # newer-commit-reachable note path.
        git(self.repo, "add", "-f", str(archived_tasks.relative_to(self.repo)))
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

        record = self.opsx_plan.state_mod.rec(self.state, self.cid)
        self.assertEqual(record["status"], self.opsx_plan.base.DONE)
        self.assertEqual(record["phase"], "done")
        ok, why = self.opsx_plan.verify_direct_archive_done(self.repo, self.cid, record)
        self.assertTrue(ok, why)

    def test_reconcile_recovers_interrupted_review_from_plan_state(self) -> None:
        record = self.opsx_plan.state_mod.rec(self.state, self.cid)
        record["status"] = self.opsx_plan.base.RUNNING
        record["phase"] = "review"
        record["round"] = 2

        self.opsx_plan.reconcile(self.repo, self.cfg, self.state)

        record = self.opsx_plan.state_mod.rec(self.state, self.cid)
        self.assertEqual(record["status"], self.opsx_plan.base.PENDING)
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

        self.assertEqual(result, self.opsx_plan.base.DONE)
        self.assertEqual(
            [stage for stage, _, _ in calls],
            ["implement", "review", "implement", "review", "archive"],
        )
        self.assertEqual(self.opsx_plan.state_mod.rec(self.state, self.cid)["round"], 2)
        self.assertEqual(self.opsx_plan.state_mod.rec(self.state, self.cid)["latest_fix_prompt"], "")

    def test_review_retry_budget_exhaustion_stops_change(self) -> None:
        self.cfg["max_rounds"] = 1
        record = self.opsx_plan.state_mod.rec(self.state, self.cid)
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
        record = self.opsx_plan.state_mod.rec(self.state, self.cid)
        self.assertEqual(record["status"], self.opsx_plan.base.FAILED)
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
        record = self.opsx_plan.state_mod.rec(self.state, self.cid)
        self.assertEqual(record["status"], self.opsx_plan.base.FAILED)
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
        record = self.opsx_plan.state_mod.rec(self.state, self.cid)
        self.assertEqual(record["status"], self.opsx_plan.base.FAILED)
        self.assertEqual(record["archive"]["status"], "failed")
        self.assertIn("still exists", record["reason"])

    def test_archive_success_still_requires_fast_checks(self) -> None:
        self.opsx_plan.groundtruth.run_fast_checks = lambda repo, cfg: (False, "check failed: smoke")
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
        record = self.opsx_plan.state_mod.rec(self.state, self.cid)
        self.assertEqual(record["status"], self.opsx_plan.base.FAILED)
        self.assertEqual(record["last_result"], "post_archive_check_failed")
        self.assertIn("post-archive", record["reason"])

    def test_corrective_handoff_persists_across_review_failure_and_retry(self) -> None:
        """Prove a multi-finding corrective handoff survives state persistence,
        is supplied to the next implementer, and clears after a clean review."""
        multi_finding_handoff = (
            "CHANGE: test-cf\n"
            "FINDINGS:\n"
            "- [critical] orchestrator/opsx-plan.py: missing input metadata block\n"
            "  → must write OPSX WORKER INPUT header\n"
            "- [warning] tests/regression/: no handoff coverage\n"
            "  → add persistence regression\n"
            "CORRECTIVE GUIDANCE: Add the comment-prefixed metadata block to\n"
            "run_logged_command before spawning the worker.\n"
            "VERIFY: python3 -m pytest tests/orchestrator/test_opsx_plan.py -k handoff"
        )
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
                        "known_change_files": [],
                        "summary": "round 1 done",
                    },
                },
                {
                    "stage": "review",
                    "result": {
                        "status": "reviewed",
                        "change": self.cid,
                        "round": 1,
                        "verdict": "fail",
                        "finding_counts": {"critical": 1, "warning": 1, "note": 0},
                        "summary": "needs corrections",
                        "fix_prompt": multi_finding_handoff,
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
                        "summary": "round 2 done",
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
                        "summary": "all clean",
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
                        "summary": "archived",
                    },
                },
            ]
        )

        result = self.opsx_plan.run_direct_change(self.repo, self.cfg, self.state, self.cid)

        self.assertEqual(result, self.opsx_plan.base.DONE)
        # The retry implementer (round 2) must receive the handoff.
        retry_input = inputs[2]  # third stage dispatch (round-2 implement)
        self.assertIn("LATEST_FIX_PROMPT:", retry_input)
        self.assertIn("CHANGE:", retry_input)
        self.assertIn(multi_finding_handoff.splitlines()[0], retry_input)
        # After the clean review, latest_fix_prompt must be cleared.
        self.assertEqual(
            self.opsx_plan.state_mod.rec(self.state, self.cid)["latest_fix_prompt"], ""
        )
        # The passing review's last_review carries an empty fix_prompt,
        # confirming the handoff was cleared (not carried forward).
        self.assertEqual(
            self.opsx_plan.state_mod.rec(self.state, self.cid)["last_review"]["verdict"], "pass"
        )
        self.assertEqual(
            self.opsx_plan.state_mod.rec(self.state, self.cid)["last_review"]["fix_prompt"], ""
        )

    def test_log_metadata_exposes_handoff_without_breaking_parsing(self) -> None:
        """Prove the OPSX WORKER INPUT metadata block appears in the stage log
        and that comment-prefixed metadata does not break JSON parsing or
        trigger false failure-marker detection."""
        handoff_text = (
            "CHANGE: test-log-handoff\n"
            "FINDINGS:\n"
            "- [critical] core/phase-protocol.md: missing handoff contract\n"
            "CORRECTIVE GUIDANCE: Document the labeled sections\n"
            "VERIFY: run openspec validate\n"
        )

        # Build a log with the metadata block followed by valid JSON and a
        # failure-like phrase in the handoff that must NOT trigger detection.
        log_path = self.repo / ".opsx-plan" / "logs" / "test-metadata.log"
        log_path.parent.mkdir(parents=True, exist_ok=True)
        with open(log_path, "w", encoding="utf-8") as lf:
            lf.write("# 2026-07-30T00:00:00+00:00 implement attempt 1: ...\n")
            lf.write("# --- OPSX WORKER INPUT ---\n")
            for line in handoff_text.splitlines():
                lf.write(f"# {line}\n")
            lf.write("# --- END OPSX WORKER INPUT ---\n")
            lf.write(
                '{"status":"implemented","change":"test-cf","round":1,'
                '"progress_made":true,"completed_tasks":[],'
                '"remaining_tasks":["1.1"],"task_counts":{"complete":0,"total":1},'
                '"files_touched":[],"known_change_files":[],"summary":"ok"}\n'
            )

        payload, reason, envelope = self.opsx_plan.parse_stage_json(log_path)
        self.assertIsNotNone(payload, f"should parse JSON despite metadata: {reason}")
        self.assertEqual(payload["status"], "implemented")
        self.assertIsNone(envelope)

        # The metadata should NOT trigger provider failure markers even though
        # the handoff contains words that might match failure patterns.
        # (The _clean_log_lines function strips all # -prefixed lines.)
        lines = self.opsx_plan._clean_log_lines(log_path.read_text(encoding="utf-8"))
        failure_reason = self.opsx_plan._scan_for_failure_marker(lines)
        self.assertEqual(failure_reason, "")


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
            self.opsx_plan.planref.is_direct_mode(cfg),
            "single-change config must route through direct workers",
        )

    def test_build_config_fails_for_missing_change_dir(self) -> None:
        with self.assertRaises(self.opsx_plan.base.PlanError) as ctx:
            self.opsx_plan.build_single_change_config(self.repo, "no-such-change")
        self.assertIn("does not exist", str(ctx.exception))

    def test_build_config_fails_for_incomplete_change(self) -> None:
        cdir = self.repo / "openspec" / "changes" / "missing-tasks"
        cdir.mkdir(parents=True)
        (cdir / "proposal.md").write_text("## Why\n", encoding="utf-8")

        with self.assertRaises(self.opsx_plan.base.PlanError) as ctx:
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
        # Isolate model resolution from the real machine's home directory
        # so escalation tests control models via env vars exclusively.
        from lib.models import resolver as _resolver
        self._models_patch = mock.patch.object(
            _resolver, "USER_CONFIG_PATH",
            Path(self.tmp.name) / "unused-home" / "models.toml"
        )
        self._models_patch.start()
        self.addCleanup(self._models_patch.stop)
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
        self.cid = "add-single-runner"
        self.plan_name = f"run-{self.cid}"
        self._saved_invoke = self.opsx_plan.invoke_direct_stage
        self._saved_checks = self.opsx_plan.groundtruth.run_fast_checks

    def tearDown(self) -> None:
        self.opsx_plan.invoke_direct_stage = self._saved_invoke
        self.opsx_plan.groundtruth.run_fast_checks = self._saved_checks
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
        state = self.opsx_plan.state_mod.load_state(self.repo, self.plan_name)
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

        self.assertEqual(result, self.opsx_plan.base.DONE)
        self.assertEqual(
            [stage for stage, _, _ in calls],
            ["implement", "review", "archive"],
        )
        record = self.opsx_plan.state_mod.rec(state, self.cid)
        self.assertEqual(record["phase"], "done")
        self.assertEqual(record["status"], self.opsx_plan.base.DONE)
        self.assertEqual(record["archive"]["status"], "passed")

    def test_single_change_review_failure_retries_implement(self) -> None:
        self.write_authored_change(self.cid)
        state = self.opsx_plan.state_mod.load_state(self.repo, self.plan_name)
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

        self.assertEqual(result, self.opsx_plan.base.DONE)
        self.assertEqual(
            [stage for stage, _, _ in calls],
            ["implement", "review", "implement", "review", "archive"],
        )
        self.assertEqual(self.opsx_plan.state_mod.rec(state, self.cid)["round"], 2)
        self.assertEqual(
            self.opsx_plan.state_mod.rec(state, self.cid)["latest_fix_prompt"], ""
        )

    def test_single_change_state_persists_under_run_prefix(self) -> None:
        self.write_authored_change(self.cid)
        state = self.opsx_plan.state_mod.load_state(self.repo, self.plan_name)
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

        state_path = self.opsx_plan.state_mod.state_path(self.repo, self.plan_name)
        self.assertTrue(state_path.is_file(), f"expected state at {state_path}")

        worker_state = self.opsx_plan.worker_state_path(self.repo, self.plan_name, self.cid)
        self.assertTrue(worker_state.is_file())

    # ── 4.4 escalation dispatch tests ────────────────────────────────────

    def _escalation_stage_runner(self, payloads: list[dict]) -> tuple[list[tuple[str, int, str, str]], list[str], list[tuple[str, str, str, str]]]:
        """Like stage_runner but also captures the implement, reviewer, and
        archiver models from os.environ at each dispatch."""
        calls: list[tuple[str, int, str, str]] = []
        input_blocks: list[str] = []
        stage_models: list[tuple[str, str, str, str]] = []  # (stage, impl_model, rev_model, arch_model)

        def fake_invoke(repo: Path, cfg: dict, cid: str, stage: str, round_num: int, input_block: str):
            self.assertTrue(payloads, f"unexpected stage call: {stage}")
            payload = payloads.pop(0)
            self.assertEqual(stage, payload["stage"])
            impl_model = os.environ.get("OPSX_IMPLEMENTER_MODEL", "")
            rev_model = os.environ.get("OPSX_REVIEWER_MODEL", "")
            arch_model = os.environ.get("OPSX_ARCHIVER_MODEL", "")
            calls.append((stage, round_num, cid, impl_model))
            stage_models.append((stage, impl_model, rev_model, arch_model))
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
        return calls, input_blocks, stage_models

    def test_escalation_threshold_two_promotes_at_round_three(self) -> None:
        """4.4: threshold 2 → rounds 1-2 use base model, round 3+ uses escalation"""
        self.write_authored_change(self.cid)
        impl_model = "deepseek/deepseek-v4-basic"
        esc_model = "deepseek/deepseek-v4-ultra"
        os.environ["OPSX_IMPLEMENTER_MODEL"] = impl_model
        os.environ["OPSX_REVIEWER_MODEL"] = "github-copilot/gpt-5.4"
        os.environ["OPSX_ARCHIVER_MODEL"] = "github-copilot/gpt-5.4"
        os.environ["OPSX_CONTROLLER_MODEL"] = "github-copilot/gpt-5.4"
        os.environ["OPSX_IMPLEMENTER_ESCALATION_MODEL"] = esc_model

        state = self.opsx_plan.state_mod.load_state(self.repo, self.plan_name)
        cfg = self.opsx_plan.build_single_change_config(self.repo, self.cid)
        cfg["escalate_after_review_fails"] = 2

        # Round 1: implement succeeds, review fails → round 2
        # Round 2: implement succeeds, review fails → round 3 (escalation!)
        # Round 3: implement succeeds, review passes → archive
        calls, _, stage_models = self._escalation_stage_runner(
            [
                {
                    "stage": "implement",
                    "result": {
                        "status": "implemented", "change": self.cid, "round": 1,
                        "progress_made": True, "completed_tasks": ["1.1"],
                        "remaining_tasks": ["1.2"], "task_counts": {"complete": 1, "total": 2},
                        "files_touched": [], "known_change_files": [],
                        "summary": "r1",
                    },
                },
                {
                    "stage": "review",
                    "result": {
                        "status": "reviewed", "change": self.cid, "round": 1,
                        "verdict": "fail", "finding_counts": {"critical": 1, "warning": 0, "note": 0},
                        "summary": "review failed r1", "fix_prompt": "fix it",
                    },
                },
                {
                    "stage": "implement",
                    "result": {
                        "status": "implemented", "change": self.cid, "round": 2,
                        "progress_made": True, "completed_tasks": ["1.2"],
                        "remaining_tasks": [], "task_counts": {"complete": 2, "total": 2},
                        "files_touched": [], "known_change_files": [],
                        "summary": "r2",
                    },
                },
                {
                    "stage": "review",
                    "result": {
                        "status": "reviewed", "change": self.cid, "round": 2,
                        "verdict": "fail", "finding_counts": {"critical": 1, "warning": 0, "note": 0},
                        "summary": "review failed r2", "fix_prompt": "fix more",
                    },
                },
                {
                    "stage": "implement",
                    "result": {
                        "status": "implemented", "change": self.cid, "round": 3,
                        "progress_made": True, "completed_tasks": [],
                        "remaining_tasks": [], "task_counts": {"complete": 2, "total": 2},
                        "files_touched": [], "known_change_files": [],
                        "summary": "r3 escalated",
                    },
                },
                {
                    "stage": "review",
                    "result": {
                        "status": "reviewed", "change": self.cid, "round": 3,
                        "verdict": "pass", "finding_counts": {"critical": 0, "warning": 0, "note": 0},
                        "summary": "review passed r3", "fix_prompt": "",
                    },
                },
                {
                    "stage": "archive",
                    "archive_repo": True,
                    "result": {
                        "status": "archived", "change": self.cid,
                        "round": 3, "verdict": "pass",
                        "finding_counts": {"critical": 0, "warning": 0, "note": 0},
                        "summary": "archived", "fix_prompt": "",
                    },
                },
            ]
        )

        self.opsx_plan.run_direct_change(self.repo, cfg, state, self.cid)

        # Verify dispatch models: rounds 1-2 should use base, round 3 escalation.
        impl_calls = [(s, rn, m) for (s, rn, cid, m) in calls if s == "implement"]
        self.assertEqual(len(impl_calls), 3)
        self.assertEqual(impl_calls[0][2], impl_model, "round 1 must use base model")
        self.assertEqual(impl_calls[1][2], impl_model, "round 2 must use base model")
        self.assertEqual(impl_calls[2][2], esc_model, "round 3 must use escalation model")
        # Reviewer and archiver models must stay unchanged across all dispatches.
        rev_model = "github-copilot/gpt-5.4"
        arch_model = "github-copilot/gpt-5.4"
        for _stg, _impl_m, _rev_m, _arch_m in stage_models:
            self.assertEqual(_rev_m, rev_model, f"{_stg} dispatch must keep reviewer model unchanged")
            self.assertEqual(_arch_m, arch_model, f"{_stg} dispatch must keep archiver model unchanged")

    def test_threshold_zero_never_escalates(self) -> None:
        """4.4: threshold 0 → never escalates"""
        self.write_authored_change(self.cid)
        impl_model = "deepseek/deepseek-v4-basic"
        esc_model = "deepseek/deepseek-v4-ultra"
        rev_model = "github-copilot/gpt-5.4"
        arch_model = "github-copilot/gpt-5.4"
        os.environ["OPSX_IMPLEMENTER_MODEL"] = impl_model
        os.environ["OPSX_REVIEWER_MODEL"] = rev_model
        os.environ["OPSX_ARCHIVER_MODEL"] = arch_model
        os.environ["OPSX_CONTROLLER_MODEL"] = "github-copilot/gpt-5.4"
        os.environ["OPSX_IMPLEMENTER_ESCALATION_MODEL"] = esc_model

        state = self.opsx_plan.state_mod.load_state(self.repo, self.plan_name)
        cfg = self.opsx_plan.build_single_change_config(self.repo, self.cid)
        cfg["escalate_after_review_fails"] = 0

        calls, _, stage_models = self._escalation_stage_runner(
            [
                {
                    "stage": "implement",
                    "result": {
                        "status": "implemented", "change": self.cid, "round": 1,
                        "progress_made": True, "completed_tasks": ["1.1"],
                        "remaining_tasks": ["1.2"], "task_counts": {"complete": 1, "total": 2},
                        "files_touched": [], "known_change_files": [],
                        "summary": "r1",
                    },
                },
                {
                    "stage": "review",
                    "result": {
                        "status": "reviewed", "change": self.cid, "round": 1,
                        "verdict": "fail", "finding_counts": {"critical": 1, "warning": 0, "note": 0},
                        "summary": "fail", "fix_prompt": "fix",
                    },
                },
                {
                    "stage": "implement",
                    "result": {
                        "status": "implemented", "change": self.cid, "round": 2,
                        "progress_made": True, "completed_tasks": ["1.2"],
                        "remaining_tasks": [], "task_counts": {"complete": 2, "total": 2},
                        "files_touched": [], "known_change_files": [],
                        "summary": "r2",
                    },
                },
                {
                    "stage": "review",
                    "result": {
                        "status": "reviewed", "change": self.cid, "round": 2,
                        "verdict": "pass", "finding_counts": {"critical": 0, "warning": 0, "note": 0},
                        "summary": "pass", "fix_prompt": "",
                    },
                },
                {
                    "stage": "archive",
                    "archive_repo": True,
                    "result": {
                        "status": "archived", "change": self.cid,
                        "round": 2, "verdict": "pass",
                        "finding_counts": {"critical": 0, "warning": 0, "note": 0},
                        "summary": "archived", "fix_prompt": "",
                    },
                },
            ]
        )

        self.opsx_plan.run_direct_change(self.repo, cfg, state, self.cid)

        # All implement dispatches must use base model.
        for s, rn, cid, m in calls:
            if s == "implement":
                self.assertEqual(m, impl_model,
                                 f"round {rn} must use base model when threshold is 0")
        # Reviewer and archiver models must stay unchanged.
        for _stg, _impl_m, _rev_m, _arch_m in stage_models:
            self.assertEqual(_rev_m, rev_model, f"{_stg} dispatch must keep reviewer model unchanged")
            self.assertEqual(_arch_m, arch_model, f"{_stg} dispatch must keep archiver model unchanged")

    def test_escalation_stays_active_in_later_rounds(self) -> None:
        """4.4: escalation active — stays active in later rounds"""
        self.write_authored_change(self.cid)
        impl_model = "deepseek/deepseek-v4-basic"
        esc_model = "deepseek/deepseek-v4-ultra"
        rev_model = "github-copilot/gpt-5.4"
        arch_model = "github-copilot/gpt-5.4"
        os.environ["OPSX_IMPLEMENTER_MODEL"] = impl_model
        os.environ["OPSX_REVIEWER_MODEL"] = rev_model
        os.environ["OPSX_ARCHIVER_MODEL"] = arch_model
        os.environ["OPSX_CONTROLLER_MODEL"] = "github-copilot/gpt-5.4"
        os.environ["OPSX_IMPLEMENTER_ESCALATION_MODEL"] = esc_model

        state = self.opsx_plan.state_mod.load_state(self.repo, self.plan_name)
        cfg = self.opsx_plan.build_single_change_config(self.repo, self.cid)
        cfg["escalate_after_review_fails"] = 1
        cfg["max_rounds"] = 4

        # Round 1: implement succeeds, review fails → escalates from round 2
        # Round 2: implement succeeds (escalated), review fails → still escalated
        # Round 3: implement succeeds (escalated), review passes → archive
        calls, _, stage_models = self._escalation_stage_runner(
            [
                {
                    "stage": "implement",
                    "result": {
                        "status": "implemented", "change": self.cid, "round": 1,
                        "progress_made": True, "completed_tasks": ["1.1"],
                        "remaining_tasks": ["1.2"], "task_counts": {"complete": 1, "total": 2},
                        "files_touched": [], "known_change_files": [],
                        "summary": "r1",
                    },
                },
                {
                    "stage": "review",
                    "result": {
                        "status": "reviewed", "change": self.cid, "round": 1,
                        "verdict": "fail", "finding_counts": {"critical": 1, "warning": 0, "note": 0},
                        "summary": "fail r1", "fix_prompt": "fix",
                    },
                },
                {
                    "stage": "implement",
                    "result": {
                        "status": "implemented", "change": self.cid, "round": 2,
                        "progress_made": True, "completed_tasks": ["1.2"],
                        "remaining_tasks": [], "task_counts": {"complete": 2, "total": 2},
                        "files_touched": [], "known_change_files": [],
                        "summary": "r2 escalated",
                    },
                },
                {
                    "stage": "review",
                    "result": {
                        "status": "reviewed", "change": self.cid, "round": 2,
                        "verdict": "fail", "finding_counts": {"critical": 1, "warning": 0, "note": 0},
                        "summary": "fail r2", "fix_prompt": "fix more",
                    },
                },
                {
                    "stage": "implement",
                    "result": {
                        "status": "implemented", "change": self.cid, "round": 3,
                        "progress_made": True, "completed_tasks": [],
                        "remaining_tasks": [], "task_counts": {"complete": 2, "total": 2},
                        "files_touched": [], "known_change_files": [],
                        "summary": "r3 still escalated",
                    },
                },
                {
                    "stage": "review",
                    "result": {
                        "status": "reviewed", "change": self.cid, "round": 3,
                        "verdict": "pass", "finding_counts": {"critical": 0, "warning": 0, "note": 0},
                        "summary": "pass r3", "fix_prompt": "",
                    },
                },
                {
                    "stage": "archive",
                    "archive_repo": True,
                    "result": {
                        "status": "archived", "change": self.cid,
                        "round": 3, "verdict": "pass",
                        "finding_counts": {"critical": 0, "warning": 0, "note": 0},
                        "summary": "archived", "fix_prompt": "",
                    },
                },
            ]
        )

        self.opsx_plan.run_direct_change(self.repo, cfg, state, self.cid)

        impl_calls = [(s, rn, m) for (s, rn, cid, m) in calls if s == "implement"]
        self.assertEqual(impl_calls[0][2], impl_model, "round 1 must use base model")
        self.assertEqual(impl_calls[1][2], esc_model, "round 2 must use escalation")
        self.assertEqual(impl_calls[2][2], esc_model, "round 3 must stay escalated")
        # Reviewer and archiver models must stay unchanged.
        for _stg, _impl_m, _rev_m, _arch_m in stage_models:
            self.assertEqual(_rev_m, rev_model, f"{_stg} dispatch must keep reviewer model unchanged")
            self.assertEqual(_arch_m, arch_model, f"{_stg} dispatch must keep archiver model unchanged")

    def test_no_progress_round_does_not_trigger_escalation(self) -> None:
        """4.5: no-progress round does not count toward threshold"""
        self.write_authored_change(self.cid)
        impl_model = "deepseek/deepseek-v4-basic"
        esc_model = "deepseek/deepseek-v4-ultra"
        rev_model = "github-copilot/gpt-5.4"
        arch_model = "github-copilot/gpt-5.4"
        os.environ["OPSX_IMPLEMENTER_MODEL"] = impl_model
        os.environ["OPSX_REVIEWER_MODEL"] = rev_model
        os.environ["OPSX_ARCHIVER_MODEL"] = arch_model
        os.environ["OPSX_CONTROLLER_MODEL"] = "github-copilot/gpt-5.4"
        os.environ["OPSX_IMPLEMENTER_ESCALATION_MODEL"] = esc_model

        state = self.opsx_plan.state_mod.load_state(self.repo, self.plan_name)
        cfg = self.opsx_plan.build_single_change_config(self.repo, self.cid)
        cfg["escalate_after_review_fails"] = 1

        # Round 1: implement with no progress → review passes → archive
        # (no review failure, so round does not advance, escalation does not trigger)
        calls, _, stage_models = self._escalation_stage_runner(
            [
                {
                    "stage": "implement",
                    "result": {
                        "status": "implemented", "change": self.cid, "round": 1,
                        "progress_made": False, "completed_tasks": [],
                        "remaining_tasks": ["1.1", "1.2"],
                        "task_counts": {"complete": 0, "total": 2},
                        "files_touched": [], "known_change_files": [],
                        "summary": "no progress",
                    },
                },
                {
                    "stage": "review",
                    "result": {
                        "status": "reviewed", "change": self.cid, "round": 1,
                        "verdict": "pass", "finding_counts": {"critical": 0, "warning": 0, "note": 0},
                        "summary": "pass", "fix_prompt": "",
                    },
                },
                {
                    "stage": "archive",
                    "archive_repo": True,
                    "result": {
                        "status": "archived", "change": self.cid,
                        "round": 1, "verdict": "pass",
                        "finding_counts": {"critical": 0, "warning": 0, "note": 0},
                        "summary": "archived", "fix_prompt": "",
                    },
                },
            ]
        )

        self.opsx_plan.run_direct_change(self.repo, cfg, state, self.cid)

        # Implement dispatch must use base model (escalation never triggered).
        impl_calls = [(s, rn, m) for (s, rn, cid, m) in calls if s == "implement"]
        self.assertEqual(len(impl_calls), 1)
        self.assertEqual(impl_calls[0][2], impl_model,
                         "no-progress round must not trigger escalation")
        # Reviewer and archiver models must stay unchanged.
        for _stg, _impl_m, _rev_m, _arch_m in stage_models:
            self.assertEqual(_rev_m, rev_model, f"{_stg} dispatch must keep reviewer model unchanged")
            self.assertEqual(_arch_m, arch_model, f"{_stg} dispatch must keep archiver model unchanged")

    def test_custom_implement_invoke_still_escalates(self) -> None:
        """4.5: custom implement_invoke still gets escalation model"""
        self.write_authored_change(self.cid)
        impl_model = "deepseek/deepseek-v4-basic"
        esc_model = "deepseek/deepseek-v4-ultra"
        rev_model = "github-copilot/gpt-5.4"
        arch_model = "github-copilot/gpt-5.4"
        os.environ["OPSX_IMPLEMENTER_MODEL"] = impl_model
        os.environ["OPSX_REVIEWER_MODEL"] = rev_model
        os.environ["OPSX_ARCHIVER_MODEL"] = arch_model
        os.environ["OPSX_CONTROLLER_MODEL"] = "github-copilot/gpt-5.4"
        os.environ["OPSX_IMPLEMENTER_ESCALATION_MODEL"] = esc_model

        state = self.opsx_plan.state_mod.load_state(self.repo, self.plan_name)
        cfg = self.opsx_plan.build_single_change_config(self.repo, self.cid)
        cfg["escalate_after_review_fails"] = 1
        cfg["implement_invoke"] = "my-tool $OPSX_IMPLEMENTER_MODEL {change}"

        # Round 1: implement succeeds, review fails → round 2 escalates
        calls, _, stage_models = self._escalation_stage_runner(
            [
                {
                    "stage": "implement",
                    "result": {
                        "status": "implemented", "change": self.cid, "round": 1,
                        "progress_made": True, "completed_tasks": ["1.1"],
                        "remaining_tasks": ["1.2"], "task_counts": {"complete": 1, "total": 2},
                        "files_touched": [], "known_change_files": [],
                        "summary": "r1",
                    },
                },
                {
                    "stage": "review",
                    "result": {
                        "status": "reviewed", "change": self.cid, "round": 1,
                        "verdict": "fail", "finding_counts": {"critical": 1, "warning": 0, "note": 0},
                        "summary": "fail", "fix_prompt": "fix",
                    },
                },
                {
                    "stage": "implement",
                    "result": {
                        "status": "implemented", "change": self.cid, "round": 2,
                        "progress_made": True, "completed_tasks": ["1.2"],
                        "remaining_tasks": [], "task_counts": {"complete": 2, "total": 2},
                        "files_touched": [], "known_change_files": [],
                        "summary": "r2 escalated",
                    },
                },
                {
                    "stage": "review",
                    "result": {
                        "status": "reviewed", "change": self.cid, "round": 2,
                        "verdict": "pass", "finding_counts": {"critical": 0, "warning": 0, "note": 0},
                        "summary": "pass", "fix_prompt": "",
                    },
                },
                {
                    "stage": "archive",
                    "archive_repo": True,
                    "result": {
                        "status": "archived", "change": self.cid,
                        "round": 2, "verdict": "pass",
                        "finding_counts": {"critical": 0, "warning": 0, "note": 0},
                        "summary": "archived", "fix_prompt": "",
                    },
                },
            ]
        )

        self.opsx_plan.run_direct_change(self.repo, cfg, state, self.cid)

        impl_calls = [(s, rn, m) for (s, rn, cid, m) in calls if s == "implement"]
        self.assertEqual(impl_calls[0][2], impl_model, "round 1 must use base model")
        self.assertEqual(impl_calls[1][2], esc_model,
                         "round 2 must use escalation with custom invoke")
        # Reviewer and archiver models must stay unchanged.
        for _stg, _impl_m, _rev_m, _arch_m in stage_models:
            self.assertEqual(_rev_m, rev_model, f"{_stg} dispatch must keep reviewer model unchanged")
            self.assertEqual(_arch_m, arch_model, f"{_stg} dispatch must keep archiver model unchanged")

    def test_non_escalated_dispatch_restores_cfg_model_after_escalation(self) -> None:
        """Regression: after a prior change escalates and leaves
        OPSX_IMPLEMENTER_MODEL in the env set to the escalation model, an
        un-escalated change must restore the cfg-resolved base model."""
        cid_a = "change-escalates"
        cid_b = "change-normal"
        self.write_authored_change(cid_a)
        self.write_authored_change(cid_b)

        impl_model = "deepseek/deepseek-v4-basic"
        esc_model = "deepseek/deepseek-v4-ultra"
        os.environ["OPSX_IMPLEMENTER_MODEL"] = impl_model
        os.environ["OPSX_REVIEWER_MODEL"] = "github-copilot/gpt-5.4"
        os.environ["OPSX_ARCHIVER_MODEL"] = "github-copilot/gpt-5.4"
        os.environ["OPSX_CONTROLLER_MODEL"] = "github-copilot/gpt-5.4"
        os.environ["OPSX_IMPLEMENTER_ESCALATION_MODEL"] = esc_model

        # ── Run change A that escalates ──
        plan_name_a = f"run-{cid_a}"
        state_a = self.opsx_plan.state_mod.load_state(self.repo, plan_name_a)
        cfg_a = self.opsx_plan.build_single_change_config(self.repo, cid_a)
        cfg_a["name"] = plan_name_a
        cfg_a["escalate_after_review_fails"] = 1

        impl_calls: list[tuple[str, int, str]] = []

        # Track reviews per change so we fail round 1 only for change A.
        review_counts: dict[str, int] = {}

        def fake_invoke(repo, cfg, cid, stage, round_num, input_block):
            impl_m = os.environ.get("OPSX_IMPLEMENTER_MODEL", "")
            if stage == "implement":
                impl_calls.append((cid, round_num, impl_m))
            log_path = self.opsx_plan.next_stage_log_path(repo, cid, stage, round_num)
            log_path.parent.mkdir(parents=True, exist_ok=True)
            if stage == "implement":
                body = json.dumps({
                    "status": "implemented", "change": cid, "round": round_num,
                    "progress_made": True, "completed_tasks": ["1.1"],
                    "remaining_tasks": [], "task_counts": {"complete": 1, "total": 1},
                    "files_touched": [], "known_change_files": [],
                    "summary": f"impl r{round_num}",
                }) + "\n"
            elif stage == "review":
                review_counts.setdefault(cid, 0)
                review_counts[cid] += 1
                # Fail only change A's first review (so escalation activates).
                if cid == cid_a and review_counts[cid] == 1:
                    body = json.dumps({
                        "status": "reviewed", "change": cid, "round": round_num,
                        "verdict": "fail",
                        "finding_counts": {"critical": 1, "warning": 0, "note": 0},
                        "summary": "fail", "fix_prompt": "fix",
                    }) + "\n"
                else:
                    body = json.dumps({
                        "status": "reviewed", "change": cid, "round": round_num,
                        "verdict": "pass",
                        "finding_counts": {"critical": 0, "warning": 0, "note": 0},
                        "summary": "pass", "fix_prompt": "",
                    }) + "\n"
            else:  # archive
                archive_path, commit = self.archive_change_in_repo(cid)
                body = json.dumps({
                    "status": "archived", "change": cid, "round": round_num,
                    "verdict": "pass",
                    "finding_counts": {"critical": 0, "warning": 0, "note": 0},
                    "summary": "ok", "fix_prompt": "",
                }) + "\n"
            log_path.write_text(body, encoding="utf-8")
            return "exited", log_path

        self.opsx_plan.invoke_direct_stage = fake_invoke
        self.opsx_plan.groundtruth.run_fast_checks = lambda _repo, _cfg: (True, "")
        self.opsx_plan.run_direct_change(self.repo, cfg_a, state_a, cid_a)

        # Verify change A escalated on its second implement dispatch.
        impl_a = [c for c in impl_calls if c[0] == cid_a]
        self.assertEqual(impl_a[0][2], impl_model,
                         "round 1 must use base model")
        self.assertEqual(impl_a[1][2], esc_model,
                         "round 2 must use escalation model")
        # os.environ now holds the escalation model (leaked state).
        self.assertEqual(os.environ["OPSX_IMPLEMENTER_MODEL"], esc_model)

        # ── Run change B (un-escalated); cfg['models']['implementer'].model is
        #    the immutable base.  Simulate the cmd_run flow where apply_model_env
        #    was called once at startup (with the base model) and then escalation
        #    overwrote the env.  Build cfg_b independently with the base model
        #    so its resolved models entry is the non-escalated value. ──
        plan_name_b = f"run-{cid_b}"
        state_b = self.opsx_plan.state_mod.load_state(self.repo, plan_name_b)
        # Reset env to base so resolve_models picks up the correct base model,
        # then apply_model_env encodes it in cfg_b.
        os.environ["OPSX_IMPLEMENTER_MODEL"] = impl_model
        cfg_b = self.opsx_plan.build_single_change_config(self.repo, cid_b)
        cfg_b["name"] = plan_name_b
        # Now corrupt the env with the escalation model to simulate leaked state.
        os.environ["OPSX_IMPLEMENTER_MODEL"] = esc_model

        self.opsx_plan.run_direct_change(self.repo, cfg_b, state_b, cid_b)

        impl_b = [c for c in impl_calls if c[0] == cid_b]
        self.assertEqual(len(impl_b), 1)
        self.assertEqual(
            impl_b[0][2], impl_model,
            "un-escalated change must restore cfg-resolved base model, "
            "not the leaked env escalation model",
        )


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
            budget_usd: float = 0.0,
        ) -> str:
            self.assertIsNone(budget_deadline)
            self.assertEqual(budget_usd, 0.0)
            calls.append((repo, cfg["name"], cid))
            return self.opsx_plan.base.DONE

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
            budget_usd: float = 0.0,
        ) -> str:
            self.assertEqual(repo, self.repo.resolve())
            self.assertIsNone(budget_deadline)
            self.assertEqual(budget_usd, 0.0)
            record = self.opsx_plan.state_mod.rec(state, cid)
            record["phase"] = "implement"
            self.opsx_plan.state_mod.set_status(
                state,
                cid,
                self.opsx_plan.base.FAILED,
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
            self.opsx_plan.base.ADAPTER_DEFAULTS["opencode"]["implement_invoke"],
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
    """The opsx-drive command surface has been removed (remove-legacy-drive-mode).
    Verify the orchestrator defaults no longer reference the legacy invoke pattern
    and that direct dispatch is the only execution path."""

    def test_opsx_plan_defaults_have_no_legacy_invoke(self) -> None:
        self.opsx_plan = load_opsx_plan()

        defaults = self.opsx_plan.base.ADAPTER_DEFAULTS["opencode"]
        self.assertNotIn(
            "invoke", defaults,
            "ADAPTER_DEFAULTS must not contain legacy invoke key after remove-legacy-drive-mode",
        )

    def test_opsx_plan_defaults_route_through_direct_workers(self) -> None:
        self.opsx_plan = load_opsx_plan()

        defaults = self.opsx_plan.base.ADAPTER_DEFAULTS["opencode"]
        self.assertIn(
            "implement_invoke", defaults,
            "default OpenCode config must route through direct workers",
        )
        self.assertIn(
            "review_invoke", defaults,
            "default OpenCode config must have review invoke",
        )
        self.assertIn(
            "archive_invoke", defaults,
            "default OpenCode config must have archive invoke",
        )

        cfg = {"adapter": "opencode", **defaults}
        self.assertTrue(
            self.opsx_plan.planref.is_direct_mode(cfg),
            "default OpenCode config must route through direct workers",
        )


class IsDirectModeGateTests(unittest.TestCase):
    def setUp(self) -> None:
        self.opsx_plan = load_opsx_plan()

    def test_opencode_plan_takes_direct_path_with_no_manifest_change(self) -> None:
        defaults = self.opsx_plan.base.ADAPTER_DEFAULTS["opencode"]
        cfg = {"adapter": "opencode", **defaults}
        self.assertTrue(
            self.opsx_plan.planref.is_direct_mode(cfg),
            "OpenCode plan must still take the direct path unchanged",
        )

    def test_plan_missing_a_stage_invoke_is_not_direct_mode(self) -> None:
        cfg = {
            "adapter": "claude-code",
            "implement_invoke": "claude -p --agent opsx-implementer",
            "review_invoke": "claude -p --agent opsx-reviewer",
            "archive_invoke": "",
        }
        self.assertFalse(
            self.opsx_plan.planref.is_direct_mode(cfg),
            "a plan missing one of the three stage invokes must not take the direct path",
        )

    def test_gate_is_independent_of_adapter_identity(self) -> None:
        cfg = {
            "adapter": "codex-cli",
            "implement_invoke": "codex exec --agent opsx-implementer",
            "review_invoke": "codex exec --agent opsx-reviewer",
            "archive_invoke": "codex exec --agent opsx-archiver",
        }
        self.assertTrue(
            self.opsx_plan.planref.is_direct_mode(cfg),
            "the gate must be configuration-driven, not conditioned on adapter identity",
        )

    def test_codex_plan_without_stage_invokes_fails_closed_naming_all_three_keys(self) -> None:
        tmp = tempfile.TemporaryDirectory()
        try:
            repo = Path(tmp.name)
            plan = repo / "codex.toml"
            plan.write_text(
                '[plan]\nname = "codex-test"\nadapter = "codex-cli"\n\n'
                '[[changes]]\nid = "c1"\n',
                encoding="utf-8",
            )
            with self.assertRaises(self.opsx_plan.base.PlanError) as ctx:
                self.opsx_plan.planref.load_plan(plan, repo=repo)
            self.assertIn("implement_invoke", str(ctx.exception))
            self.assertIn("review_invoke", str(ctx.exception))
            self.assertIn("archive_invoke", str(ctx.exception))
        finally:
            tmp.cleanup()

    def test_codex_cli_adapter_defaults_have_no_legacy_invoke(self) -> None:
        codex_defaults = self.opsx_plan.base.ADAPTER_DEFAULTS.get("codex-cli", {})
        self.assertNotIn(
            "invoke", codex_defaults,
            "codex-cli ADAPTER_DEFAULTS must not contain legacy invoke key",
        )
        self.assertNotIn(
            "max_attempts", codex_defaults,
            "codex-cli ADAPTER_DEFAULTS must not contain legacy max_attempts key",
        )


class ClaudeCodeAdapterDefaultsTests(unittest.TestCase):
    def setUp(self) -> None:
        self.opsx_plan = load_opsx_plan()
        self.tmp = tempfile.TemporaryDirectory()
        self.repo = Path(self.tmp.name)

    def tearDown(self) -> None:
        self.tmp.cleanup()

    def _write_plan(self, name: str, body: str) -> Path:
        p = self.repo / name
        p.write_text(body, encoding="utf-8")
        return p

    def test_claude_code_plan_resolves_all_three_defaults_and_takes_direct_path(self) -> None:
        plan = self._write_plan(
            "plan.toml",
            '[plan]\nname = "test"\nadapter = "claude-code"\n\n'
            '[[changes]]\nid = "c1"\n',
        )
        cfg = self.opsx_plan.planref.load_plan(plan)

        for stage, env_var in (
            ("implement", "OPSX_IMPLEMENTER_MODEL"),
            ("review", "OPSX_REVIEWER_MODEL"),
            ("archive", "OPSX_ARCHIVER_MODEL"),
        ):
            invoke = cfg[f"{stage}_invoke"]
            self.assertIn("claude", invoke)
            self.assertIn(f"opsx-{stage}er" if stage != "archive" else "opsx-archiver", invoke)
            self.assertIn(f"${env_var}", invoke)
            self.assertIn("--permission-mode bypassPermissions", invoke)
            self.assertIn("--output-format json", invoke)

        self.assertTrue(
            self.opsx_plan.planref.is_direct_mode(cfg),
            "claude-code plan with no invoke overrides must resolve to the direct path",
        )

    def test_single_overridden_stage_invoke_is_honored_others_fall_back(self) -> None:
        defaults = self.opsx_plan.base.ADAPTER_DEFAULTS["claude-code"]
        plan = self._write_plan(
            "plan.toml",
            '[plan]\nname = "test"\nadapter = "claude-code"\n'
            'review_invoke = "claude -p --agent custom-reviewer --output-format json"\n\n'
            '[[changes]]\nid = "c1"\n',
        )
        cfg = self.opsx_plan.planref.load_plan(plan)

        self.assertEqual(
            cfg["review_invoke"],
            "claude -p --agent custom-reviewer --output-format json",
        )
        self.assertEqual(cfg["implement_invoke"], defaults["implement_invoke"])
        self.assertEqual(cfg["archive_invoke"], defaults["archive_invoke"])
        self.assertTrue(self.opsx_plan.planref.is_direct_mode(cfg))


class ModelResolutionWiringTests(unittest.TestCase):
    """7.4, 7.7: load_plan populates cfg["models"] per adapter, and the
    incomplete-direct-dispatch guard ensures incomplete configs fail closed
    with all three required keys named."""

    def setUp(self) -> None:
        self.opsx_plan = load_opsx_plan()
        self.tmp = tempfile.TemporaryDirectory()
        self.repo = Path(self.tmp.name)
        # Save env vars so apply_model_env side effects don't leak.
        self._saved_env = {}
        for key in ("OPSX_IMPLEMENTER_MODEL", "OPSX_IMPLEMENTER_ESCALATION_MODEL",
                     "OPSX_REVIEWER_MODEL", "OPSX_ARCHIVER_MODEL",
                     "OPSX_CONTROLLER_MODEL"):
            self._saved_env[key] = os.environ.get(key)
        # Isolate model resolution from the real machine's home directory.
        from lib.models import resolver as _resolver
        self._models_patch = mock.patch.object(
            _resolver, "USER_CONFIG_PATH", Path(self.tmp.name) / "unused-home" / "models.toml"
        )
        self._models_patch.start()
        self.addCleanup(self._models_patch.stop)

    def tearDown(self) -> None:
        # Restore env vars that apply_model_env may have modified.
        for key, val in self._saved_env.items():
            if val is not None:
                os.environ[key] = val
            else:
                os.environ.pop(key, None)
        self.tmp.cleanup()

    def _write_config(self, content: str) -> None:
        cfg_path = self.repo / ".opsx-plan" / "models.toml"
        cfg_path.parent.mkdir(parents=True, exist_ok=True)
        cfg_path.write_text(textwrap.dedent(content), encoding="utf-8")

    def _write_plan(self, name: str, adapter: str) -> Path:
        p = self.repo / name
        p.write_text(
            f'[plan]\nname = "test"\nadapter = "{adapter}"\n\n[[changes]]\nid = "c1"\n',
            encoding="utf-8",
        )
        return p

    def test_load_plan_populates_cfg_models_for_all_roles(self) -> None:
        self._write_config(
            """\
            [adapters.opencode]
            implementer = "deepseek/deepseek-v4-pro"
            controller = "github-copilot/gpt-5.4"
            reviewer = "github-copilot/gpt-5.4"
            archiver = "github-copilot/gpt-5.4"
            """
        )
        plan = self._write_plan("plan.toml", "opencode")
        cfg = self.opsx_plan.planref.load_plan(plan, repo=self.repo)

        self.assertIn("models", cfg)
        for role in self.opsx_plan.ROLES:
            self.assertIn(role, cfg["models"])
        self.assertEqual(cfg["models"]["implementer"].model, "deepseek/deepseek-v4-pro")

    def test_two_adapters_resolve_different_identifiers_from_one_config_file(self) -> None:
        self._write_config(
            """\
            [adapters.opencode]
            implementer = "deepseek/deepseek-v4-pro"

            [adapters.claude-code]
            implementer = "claude-sonnet-5"
            """
        )
        opencode_plan = self._write_plan("opencode.toml", "opencode")
        claude_plan = self._write_plan("claude.toml", "claude-code")

        opencode_cfg = self.opsx_plan.planref.load_plan(opencode_plan, repo=self.repo)
        claude_cfg = self.opsx_plan.planref.load_plan(claude_plan, repo=self.repo)

        self.assertEqual(opencode_cfg["models"]["implementer"].model, "deepseek/deepseek-v4-pro")
        self.assertEqual(claude_cfg["models"]["implementer"].model, "claude-sonnet-5")

    def test_incomplete_direct_dispatch_fails_closed_with_all_three_keys(self) -> None:
        plan = self.repo / "incomplete.toml"
        plan.write_text(
            '[plan]\nname = "incomplete"\nadapter = "claude-code"\n'
            'implement_invoke = ""\nreview_invoke = ""\narchive_invoke = ""\n\n'
            '[[changes]]\nid = "c1"\n',
            encoding="utf-8",
        )
        with self.assertRaises(self.opsx_plan.base.PlanError) as ctx:
            self.opsx_plan.planref.load_plan(plan, repo=self.repo)
        self.assertIn("implement_invoke", str(ctx.exception))
        self.assertIn("review_invoke", str(ctx.exception))
        self.assertIn("archive_invoke", str(ctx.exception))

    def test_escalate_after_review_fails_defaults_to_zero(self) -> None:
        """2.6: absent key → 0"""
        plan = self._write_plan("plan.toml", "opencode")
        cfg = self.opsx_plan.planref.load_plan(plan, repo=self.repo)
        self.assertEqual(cfg["escalate_after_review_fails"], 0)

    def test_escalate_after_review_fails_negative_value_raises(self) -> None:
        """2.6: negative value raises PlanError naming the key"""
        plan = self.repo / "neg.toml"
        plan.write_text(
            '[plan]\nname = "neg"\nadapter = "opencode"\n'
            'escalate_after_review_fails = -1\n\n'
            '[[changes]]\nid = "c1"\n',
            encoding="utf-8",
        )
        with self.assertRaises(self.opsx_plan.base.PlanError) as ctx:
            self.opsx_plan.planref.load_plan(plan, repo=self.repo)
        self.assertIn("escalate_after_review_fails", str(ctx.exception))

    def test_finding_recurrence_limit_defaults_to_zero(self) -> None:
        """4.1: absent key -> 0 (recurrence halting disabled)"""
        plan = self._write_plan("plan.toml", "opencode")
        cfg = self.opsx_plan.planref.load_plan(plan, repo=self.repo)
        self.assertEqual(cfg["finding_recurrence_limit"], 0)

    def test_finding_recurrence_limit_negative_value_raises(self) -> None:
        """4.1: negative value raises PlanError naming the key"""
        plan = self.repo / "neg-recurrence.toml"
        plan.write_text(
            '[plan]\nname = "neg-recurrence"\nadapter = "opencode"\n'
            'finding_recurrence_limit = -1\n\n'
            '[[changes]]\nid = "c1"\n',
            encoding="utf-8",
        )
        with self.assertRaises(self.opsx_plan.base.PlanError) as ctx:
            self.opsx_plan.planref.load_plan(plan, repo=self.repo)
        self.assertIn("finding_recurrence_limit", str(ctx.exception))

    def test_apply_model_env_succeeds_with_unresolved_escalation_and_threshold_zero(self) -> None:
        """3.3: escalation unresolved + threshold 0 → no error"""
        os.environ.pop("OPSX_IMPLEMENTER_ESCALATION_MODEL", None)
        self._write_config(
            """\
            [adapters.opencode]
            controller = "github-copilot/gpt-5.4"
            implementer = "deepseek/deepseek-v4-pro"
            reviewer = "github-copilot/gpt-5.4"
            archiver = "github-copilot/gpt-5.4"
            """
        )
        plan = self._write_plan("plan.toml", "opencode")
        cfg = self.opsx_plan.planref.load_plan(plan, repo=self.repo)
        # Threshold 0, escalation unresolved — must not raise.
        models = cfg.get("models", {})
        self.assertIsNone(models["implementer_escalation"].model,
                          "escalation must be unresolved for this test")
        self.opsx_plan.apply_model_env(cfg)

    def test_apply_model_env_raises_when_threshold_positive_and_escalation_unresolved(self) -> None:
        """3.3: threshold > 0 + unresolved escalation → PlanError"""
        os.environ.pop("OPSX_IMPLEMENTER_ESCALATION_MODEL", None)
        self._write_config(
            """\
            [adapters.opencode]
            controller = "github-copilot/gpt-5.4"
            implementer = "deepseek/deepseek-v4-pro"
            reviewer = "github-copilot/gpt-5.4"
            archiver = "github-copilot/gpt-5.4"
            """
        )
        plan_path = self.repo / "esc.toml"
        plan_path.write_text(
            '[plan]\nname = "esc"\nadapter = "opencode"\n'
            'escalate_after_review_fails = 2\n\n'
            '[[changes]]\nid = "c1"\n',
            encoding="utf-8",
        )
        cfg = self.opsx_plan.planref.load_plan(plan_path, repo=self.repo)
        with self.assertRaises(self.opsx_plan.base.PlanError) as ctx:
            self.opsx_plan.apply_model_env(cfg)
        self.assertIn("implementer_escalation", str(ctx.exception))
        self.assertIn("unresolved", str(ctx.exception).lower())

    def test_apply_model_env_exports_escalation_variable_when_resolved(self) -> None:
        """3.3: resolved escalation role → OPSX_IMPLEMENTER_ESCALATION_MODEL exported"""
        self._write_config(
            """\
            [adapters.opencode]
            controller = "github-copilot/gpt-5.4"
            implementer = "deepseek/deepseek-v4-pro"
            implementer_escalation = "deepseek/deepseek-v4-ultra"
            reviewer = "github-copilot/gpt-5.4"
            archiver = "github-copilot/gpt-5.4"
            """
        )
        plan = self._write_plan("plan.toml", "opencode")
        cfg = self.opsx_plan.planref.load_plan(plan, repo=self.repo)
        self.assertEqual(cfg["escalate_after_review_fails"], 0)
        self.opsx_plan.apply_model_env(cfg)
        self.assertEqual(
            os.environ.get("OPSX_IMPLEMENTER_ESCALATION_MODEL"),
            "deepseek/deepseek-v4-ultra",
        )

    def test_apply_model_env_unsets_stale_escalation_when_unresolved(self) -> None:
        """3.3: stale OPSX_IMPLEMENTER_ESCALATION_MODEL is unset when
        the escalation role is unresolved (regression: a prior
        apply_model_env call must not leak into a subsequent
        non-escalation dispatch)."""
        self._write_config(
            """\
            [adapters.opencode]
            controller = "github-copilot/gpt-5.4"
            implementer = "deepseek/deepseek-v4-pro"
            reviewer = "github-copilot/gpt-5.4"
            archiver = "github-copilot/gpt-5.4"
            """
        )
        plan = self._write_plan("plan.toml", "opencode")
        # Resolve models while escalation env is absent so the role
        # lands as unresolved in cfg["models"].
        os.environ.pop("OPSX_IMPLEMENTER_ESCALATION_MODEL", None)
        cfg = self.opsx_plan.planref.load_plan(plan, repo=self.repo)
        # Now simulate a stale leftover from a prior apply_model_env call
        # on a different config that *did* have an escalation model.
        os.environ["OPSX_IMPLEMENTER_ESCALATION_MODEL"] = "stale/leftover-model"
        self.opsx_plan.apply_model_env(cfg)
        self.assertNotIn(
            "OPSX_IMPLEMENTER_ESCALATION_MODEL",
            os.environ,
            "stale escalation env var must be unset when escalation is unresolved",
        )


class InvokeDirectStageEnvExpansionTests(unittest.TestCase):
    def setUp(self) -> None:
        self.opsx_plan = load_opsx_plan()
        self.tmp = tempfile.TemporaryDirectory()
        self.repo = Path(self.tmp.name)

    def tearDown(self) -> None:
        self.tmp.cleanup()

    def _cfg(self, invoke: str) -> dict:
        return {
            "implement_invoke": invoke,
            "changes": {"c1": {"timeout_minutes": 1}},
        }

    def test_successful_expansion_reaches_subprocess(self) -> None:
        os.environ["OPSX_TEST_EXPANSION_MODEL"] = "sonnet-test"
        try:
            cfg = self._cfg('echo --model "$OPSX_TEST_EXPANSION_MODEL"')
            outcome, log_path = self.opsx_plan.invoke_direct_stage(
                self.repo, cfg, "c1", "implement", 1, "INPUT_BLOCK"
            )
        finally:
            del os.environ["OPSX_TEST_EXPANSION_MODEL"]

        self.assertEqual(outcome, "exited")
        content = log_path.read_text(encoding="utf-8")
        self.assertIn("sonnet-test", content)
        self.assertNotIn("$OPSX_TEST_EXPANSION_MODEL", content)

    def test_unset_variable_fails_before_spawning(self) -> None:
        var = "OPSX_TEST_MISSING_MODEL_VAR"
        os.environ.pop(var, None)
        cfg = self._cfg(f'echo --model "${var}"')
        outcome, log_path = self.opsx_plan.invoke_direct_stage(
            self.repo, cfg, "c1", "implement", 1, "INPUT_BLOCK"
        )
        self.assertEqual(outcome, "env_error")
        content = log_path.read_text(encoding="utf-8")
        self.assertIn(var, content)

    def test_invoke_with_no_variable_references(self) -> None:
        cfg = self._cfg("echo hello world")
        outcome, log_path = self.opsx_plan.invoke_direct_stage(
            self.repo, cfg, "c1", "implement", 1, "INPUT_BLOCK"
        )
        self.assertEqual(outcome, "exited")
        content = log_path.read_text(encoding="utf-8")
        self.assertIn("hello world", content)

    def test_input_block_is_trailing_positional_argument(self) -> None:
        argv_dump = self.repo / "argv.json"
        script = (
            "import sys, json, pathlib; "
            f"pathlib.Path({str(argv_dump)!r}).write_text(json.dumps(sys.argv))"
        )
        cfg = self._cfg(f"python3 -c {shlex.quote(script)}")
        input_block = "THE INPUT BLOCK\nwith multiple lines"
        outcome, _log_path = self.opsx_plan.invoke_direct_stage(
            self.repo, cfg, "c1", "implement", 1, input_block
        )
        self.assertEqual(outcome, "exited")
        argv = json.loads(argv_dump.read_text(encoding="utf-8"))
        self.assertEqual(argv[-1], input_block)
        self.assertEqual(len(argv), 2)

    def test_exec_log_line_shows_command_with_input_block_elided(self) -> None:
        cfg = self._cfg('echo --model "$OPSX_TEST_EXPANSION_MODEL_2"')
        os.environ["OPSX_TEST_EXPANSION_MODEL_2"] = "sonnet-test-2"
        input_block = "THIS SHOULD NOT APPEAR IN THE EXEC LOG LINE"
        try:
            with mock.patch("sys.stdout", io.StringIO()) as stdout:
                outcome, _log_path = self.opsx_plan.invoke_direct_stage(
                    self.repo, cfg, "c1", "implement", 1, input_block
                )
        finally:
            del os.environ["OPSX_TEST_EXPANSION_MODEL_2"]

        self.assertEqual(outcome, "exited")
        printed = stdout.getvalue()
        exec_lines = [line for line in printed.splitlines() if "exec[implement]" in line]
        self.assertEqual(len(exec_lines), 1)
        exec_line = exec_lines[0]
        self.assertIn("sonnet-test-2", exec_line)
        self.assertNotIn(input_block, exec_line)
        self.assertIn("<input>", exec_line)

    def test_stage_log_header_elides_multiline_input_block(self) -> None:
        cfg = self._cfg("true")
        input_block = "THE INPUT BLOCK\nwith multiple lines\nand a third line"
        outcome, log_path = self.opsx_plan.invoke_direct_stage(
            self.repo, cfg, "c1", "implement", 1, input_block
        )
        self.assertEqual(outcome, "exited")
        content = log_path.read_text(encoding="utf-8")
        header_line = content.splitlines()[0]
        self.assertIn("<input>", header_line)
        self.assertIn("attempt 1", header_line)
        # The multi-line input block is written as comment-prefixed metadata
        # (OPSX WORKER INPUT block) so operators can inspect it. It must NOT
        # appear as ordinary log lines that could break JSON parsing.
        self.assertIn("# --- OPSX WORKER INPUT ---", content)
        self.assertIn("# THE INPUT BLOCK", content)
        self.assertIn("# --- END OPSX WORKER INPUT ---", content)
        # _clean_log_lines strips #-prefixed lines, so the metadata does not
        # interfere with worker result parsing.
        clean = self.opsx_plan._clean_log_lines(content)
        self.assertNotIn("THE INPUT BLOCK", "\n".join(clean))

    def test_apply_model_env_populates_variable_invoke_expands(self) -> None:
        """7.5: after apply_model_env, invoke_direct_stage expands
        $OPSX_IMPLEMENTER_MODEL to the adapter-specific resolved value."""
        # apply_model_env writes all four OPSX_*_MODEL vars into the real
        # process environment, so all four must be saved and restored.
        saved_vars = {
            var: os.environ.get(var) for var in self.opsx_plan.ROLE_ENV.values()
        }
        try:
            cfg = {
                "adapter": "claude-code",
                "models": {
                    "controller": ResolvedModel(
                        role="controller", model="claude-sonnet-5", source="test"
                    ),
                    "implementer": ResolvedModel(
                        role="implementer", model="claude-sonnet-5", source="test"
                    ),
                    "reviewer": ResolvedModel(
                        role="reviewer", model="claude-opus-5", source="test"
                    ),
                    "archiver": ResolvedModel(
                        role="archiver", model="claude-sonnet-5", source="test"
                    ),
                },
            }
            self.opsx_plan.apply_model_env(cfg)

            cfg["implement_invoke"] = 'echo --model "$OPSX_IMPLEMENTER_MODEL"'
            cfg["changes"] = {"c1": {"timeout_minutes": 1}}
            outcome, log_path = self.opsx_plan.invoke_direct_stage(
                self.repo, cfg, "c1", "implement", 1, "INPUT_BLOCK"
            )
            self.assertEqual(outcome, "exited")
            content = log_path.read_text(encoding="utf-8")
            self.assertIn("claude-sonnet-5", content)
        finally:
            for var, value in saved_vars.items():
                if value is not None:
                    os.environ[var] = value
                else:
                    os.environ.pop(var, None)


class OpenCodeAgentModeTests(unittest.TestCase):
    AGENT_DIR = Path(__file__).resolve().parents[2] / "adapters" / "opencode" / "agents"

    def test_opencode_worker_agents_are_runnable_via_run_agent(self) -> None:
        for name in (
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


class ArchiverDeletionStagingTests(unittest.TestCase):
    REPO_ROOT = Path(__file__).resolve().parents[2]
    ARCHIVER_FILES = (
        "adapters/claude-code/agents/opsx-archiver.md",
        "adapters/opencode/agents/opsx-archiver.md",
        "adapters/codex-cli/agents/opsx-archiver.toml",
        "adapters/codex-cli/plugin/agents/opsx-archiver.toml",
        "plugins/opsx-controller/agents/opsx-archiver.md",
    )
    # These four name the change directory in their scope-determination step
    # with the shared "(the deletion left by the move)" marker; the
    # claude-code definition names it inline in its staging step instead.
    SCOPE_STEP_ENUMERATES_CHANGE_DIR_FILES = (
        "adapters/opencode/agents/opsx-archiver.md",
        "adapters/codex-cli/agents/opsx-archiver.toml",
        "adapters/codex-cli/plugin/agents/opsx-archiver.toml",
        "plugins/opsx-controller/agents/opsx-archiver.md",
    )

    # The step after the deletion-staging step enumerates what else to stage.
    # Each definition must reconcile that list with the deletion already
    # staged, or its "only" reads as an instruction to exclude it. The wording
    # differs per variant, so pin the exact clause per file. Renumbering the
    # steps means updating these strings -- a stale cross-reference here is
    # exactly the drift this assertion exists to catch.
    STAGING_STEP_RECONCILES_DELETION = {
        "adapters/claude-code/agents/opsx-archiver.md": (
            "and the change-directory deletion staged in step 13."
        ),
        "adapters/opencode/agents/opsx-archiver.md": (
            "Leave the change-directory deletion from step 15 staged; "
            "do not unstage it."
        ),
        "adapters/codex-cli/agents/opsx-archiver.toml": (
            "and the change-directory deletion staged in step 12."
        ),
        "adapters/codex-cli/plugin/agents/opsx-archiver.toml": (
            "and the change-directory deletion staged in step 12."
        ),
        "plugins/opsx-controller/agents/opsx-archiver.md": (
            "Stage only the rest of the explicit archive set."
        ),
    }

    def _read(self, rel_path: str) -> str:
        return (self.REPO_ROOT / rel_path).read_text(encoding="utf-8")

    def _read_unwrapped(self, rel_path: str) -> str:
        """Read a definition with line wrapping collapsed to single spaces.

        The staging clauses wrap mid-sentence in every definition, so match
        against normalized text rather than the raw file.
        """
        return " ".join(self._read(rel_path).split())

    def test_all_archivers_stage_change_directory_deletion(self) -> None:
        for rel_path in self.ARCHIVER_FILES:
            text = self._read(rel_path)
            self.assertIn(
                "git add -A -- openspec/changes",
                text,
                f"{rel_path} must instruct staging the change-directory deletion "
                "with a git add -A pathspec so the move commits as one rename",
            )

    def test_all_archivers_require_deletion_present_before_commit(self) -> None:
        for rel_path in self.ARCHIVER_FILES:
            text = self._read(rel_path)
            self.assertIn(
                "deletions under",
                text,
                f"{rel_path} pre-commit inspection must reference the "
                "change-directory deletions",
            )
            self.assertIn(
                "absent from the staged",
                text,
                f"{rel_path} pre-commit inspection must fail closed when the "
                "change-directory deletion is missing from the staged set",
            )

    def test_scope_step_names_change_directory_in_four_definitions(self) -> None:
        for rel_path in self.SCOPE_STEP_ENUMERATES_CHANGE_DIR_FILES:
            text = self._read(rel_path)
            self.assertIn(
                "(the deletion left by the move)",
                text,
                f"{rel_path} must name openspec/changes/<change>/ as in-scope "
                "in its scope-determination step",
            )

    def test_staging_step_reconciles_the_deletion_in_every_definition(self) -> None:
        self.assertEqual(
            set(self.STAGING_STEP_RECONCILES_DELETION),
            set(self.ARCHIVER_FILES),
            "every archiver definition needs a pinned staging-step clause; "
            "add new adapters to STAGING_STEP_RECONCILES_DELETION",
        )
        for rel_path, clause in self.STAGING_STEP_RECONCILES_DELETION.items():
            self.assertIn(
                clause,
                self._read_unwrapped(rel_path),
                f"{rel_path} must reconcile its explicit-staging step with the "
                "change-directory deletion staged in the preceding step, so "
                "that step's 'only' is not read as excluding the deletion",
            )

    def test_all_archivers_guard_staging_with_git_ls_files(self) -> None:
        for rel_path in self.ARCHIVER_FILES:
            text = self._read(rel_path)
            self.assertIn(
                "git ls-files -- openspec/changes",
                text,
                f"{rel_path} must guard change-directory deletion staging "
                "with a git ls-files tracked check so git add -A is never "
                "run on an untracked pathspec",
            )

    def test_all_archivers_treat_untracked_change_dir_as_non_failure(self) -> None:
        for rel_path in self.ARCHIVER_FILES:
            text = self._read(rel_path)
            self.assertIn(
                "absent deletions are expected and are not",
                text,
                f"{rel_path} must describe an untracked change directory's "
                "absent deletions as expected, not a pre-commit failure",
            )

    def test_all_archivers_never_stage_the_archive_destination(self) -> None:
        for rel_path in self.ARCHIVER_FILES:
            text = self._read(rel_path)
            self.assertIn(
                "gitignored",
                text,
                f"{rel_path} must document that openspec/changes/archive/ is "
                "gitignored and must never be staged or committed",
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
        payload, reason, envelope = self.opsx_plan.parse_stage_json(log_path)
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
        payload, reason, envelope = self.opsx_plan.parse_stage_json(log_path)
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
        payload, reason, envelope = self.opsx_plan.parse_stage_json(log_path)
        self.assertIsNone(payload)
        self.assertIn("permission denied before JSON output", reason)
        self.assertIn("external_directory permission denied", reason)

    def test_insufficient_balance_transcript_is_parsed_as_provider_failure(self) -> None:
        content = (
            "# header\n"
            "> opsx-implementer · deepseek-v4-pro\n"
            "Error: Insufficient Balance\n"
        )
        log_path = self._write_log(content)
        payload, reason, envelope = self.opsx_plan.parse_stage_json(log_path)
        self.assertIsNone(payload)
        self.assertIn("provider failure before JSON output", reason)
        self.assertIn("Insufficient Balance", reason)

    def test_no_permission_marker_returns_generic_reason(self) -> None:
        content = (
            "some output\n"
            "more output\n"
            "nothing parseable\n"
        )
        log_path = self._write_log(content)
        payload, reason, envelope = self.opsx_plan.parse_stage_json(log_path)
        self.assertIsNone(payload)
        self.assertIn("expected a final JSON object line", reason)
        self.assertNotIn("permission denied", reason)

    def test_plain_unwrapped_output_still_parses_unchanged(self) -> None:
        content = (
            '{"status":"implemented","change":"ex","round":1,"progress_made":true,'
            '"completed_tasks":[],"remaining_tasks":[],'
            '"task_counts":{"complete":0,"total":0},'
            '"files_touched":[],"known_change_files":[],"summary":"done"}\n'
        )
        log_path = self._write_log(content)
        payload, reason, envelope = self.opsx_plan.parse_stage_json(log_path)
        self.assertIsNotNone(payload)
        self.assertEqual(payload["status"], "implemented")
        self.assertEqual(reason, "")
        self.assertIsNone(envelope)


class ClaudeCodeResultEnvelopeParsingTests(unittest.TestCase):
    def setUp(self) -> None:
        self.opsx_plan = load_opsx_plan()
        self.tmp = tempfile.TemporaryDirectory()
        self.log_dir = Path(self.tmp.name)

    def tearDown(self) -> None:
        self.tmp.cleanup()

    def _write_log(self, content: str) -> Path:
        p = self.log_dir / f"test-{id(content)}.log"
        p.write_text(content, encoding="utf-8")
        return p

    def test_worker_json_is_recovered_from_an_envelope(self) -> None:
        worker_json = (
            '{"status":"implemented","change":"ex","round":1,"progress_made":true,'
            '"completed_tasks":[],"remaining_tasks":[],'
            '"task_counts":{"complete":0,"total":0},'
            '"files_touched":[],"known_change_files":[],"summary":"done"}'
        )
        envelope_obj = {
            "type": "result",
            "result": f"Some preamble text.\n{worker_json}",
            "usage": {"input_tokens": 10, "output_tokens": 5},
        }
        content = "# header\n" + json.dumps(envelope_obj) + "\n"
        log_path = self._write_log(content)

        payload, reason, envelope = self.opsx_plan.parse_stage_json(log_path)
        self.assertIsNotNone(payload, f"expected recovered worker JSON, reason={reason}")
        self.assertEqual(payload["status"], "implemented")
        self.assertEqual(reason, "")
        self.assertIsNotNone(envelope)
        self.assertEqual(envelope["type"], "result")

    def test_worker_json_with_unclosed_inline_code_backtick_is_recovered(self) -> None:
        worker_json = (
            '{"status":"implemented","change":"ex","round":1,"progress_made":true,'
            '"completed_tasks":[],"remaining_tasks":[],'
            '"task_counts":{"complete":0,"total":0},'
            '"files_touched":[],"known_change_files":[],"summary":"done"}'
        )
        envelope_obj = {"type": "result", "result": f"`{worker_json}"}
        log_path = self._write_log(json.dumps(envelope_obj) + "\n")

        payload, reason, envelope = self.opsx_plan.parse_stage_json(log_path)
        self.assertIsNotNone(payload, f"expected recovered worker JSON, reason={reason}")
        self.assertEqual(payload["status"], "implemented")
        self.assertEqual(reason, "")
        self.assertIsNotNone(envelope)

    def test_envelope_with_no_worker_json_is_invalid_output(self) -> None:
        envelope_obj = {
            "type": "result",
            "result": "The assistant produced no final JSON line, just prose.",
        }
        content = json.dumps(envelope_obj) + "\n"
        log_path = self._write_log(content)

        payload, reason, envelope = self.opsx_plan.parse_stage_json(log_path)
        self.assertIsNone(payload)
        self.assertIn("expected a final JSON object line", reason)
        self.assertIsNotNone(envelope)

    def test_permission_rejection_inside_envelope_is_reported_actionably(self) -> None:
        envelope_obj = {
            "type": "result",
            "result": "auto-rejecting request\nThe user rejected permission for external_directory",
        }
        content = json.dumps(envelope_obj) + "\n"
        log_path = self._write_log(content)

        payload, reason, envelope = self.opsx_plan.parse_stage_json(log_path)
        self.assertIsNone(payload)
        self.assertIn("permission denied before JSON output", reason)
        self.assertIsNotNone(envelope)

    def test_unwrapping_correct_under_streamed_multiline_log(self) -> None:
        worker_json = (
            '{"status":"reviewed","change":"ex","round":1,"verdict":"pass",'
            '"finding_counts":{"critical":0,"warning":0,"note":0},'
            '"summary":"ok","fix_prompt":""}'
        )
        intermediate = {
            "type": "assistant",
            "usage": {"input_tokens": 999, "output_tokens": 999},
        }
        final_envelope = {
            "type": "result",
            "result": worker_json,
            "usage": {"input_tokens": 20, "output_tokens": 8},
        }
        content = "\n".join(
            [
                json.dumps(intermediate),
                json.dumps({"type": "assistant", "text": "partial"}),
                json.dumps(final_envelope),
            ]
        ) + "\n"
        log_path = self._write_log(content)

        payload, reason, envelope = self.opsx_plan.parse_stage_json(log_path)
        self.assertIsNotNone(payload, f"reason={reason}")
        self.assertEqual(payload["status"], "reviewed")
        self.assertEqual(envelope["usage"]["input_tokens"], 20)

    def test_stderr_side_marker_reported_when_result_text_has_no_marker(self) -> None:
        envelope_obj = {
            "type": "result",
            "result": "The assistant produced no final JSON line, just prose.",
        }
        content = (
            "The user rejected permission for external_directory\n"
            + json.dumps(envelope_obj)
            + "\n"
        )
        log_path = self._write_log(content)

        payload, reason, envelope = self.opsx_plan.parse_stage_json(log_path)
        self.assertIsNone(payload)
        self.assertIn("permission denied before JSON output", reason)
        self.assertIsNotNone(envelope)
class ActivePlanResolutionTests(unittest.TestCase):
    """Tests for active plan pointer: resolution, use, auto-activation, and
    status output (tasks 5.1–5.3)."""

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
        self._saved_environ = dict(os.environ)

    def tearDown(self) -> None:
        os.environ.clear()
        os.environ.update(self._saved_environ)
        self.tmp.cleanup()

    def _write_plan_toml(self, rel_path: str, content: str | None = None) -> Path:
        """Write a valid plan TOML that can pass load_plan."""
        p = self.repo / rel_path
        p.parent.mkdir(parents=True, exist_ok=True)
        if content is not None:
            p.write_text(content, encoding="utf-8")
        else:
            p.write_text(
                '[plan]\nname = "test-plan"\nadapter = "opencode"\n\n'
                "[[changes]]\nid = \"test-change\"\n",
                encoding="utf-8",
            )
        return p

    def _write_plan_md(self, rel_path: str, content: str | None = None) -> Path:
        p = self.repo / rel_path
        p.parent.mkdir(parents=True, exist_ok=True)
        if content is not None:
            p.write_text(content, encoding="utf-8")
        else:
            p.write_text(
                "# Plan\n\n## Phase 1\n\n### Change: `test-change`\n\n**Depends on:** None.\n",
                encoding="utf-8",
            )
        return p

    # ── 5.1: resolve_plan precedence branches ───────────────────────────

    def test_resolve_plan_returns_explicit_argument(self) -> None:
        result = self.opsx_plan.planref.resolve_plan(self.repo, "explicit-plan.toml")
        self.assertEqual(result, "explicit-plan.toml")

    def test_resolve_plan_falls_back_to_env_var(self) -> None:
        os.environ["OPSX_PLAN"] = "env-plan.toml"
        result = self.opsx_plan.planref.resolve_plan(self.repo, None)
        self.assertEqual(result, "env-plan.toml")

    def test_resolve_plan_falls_back_to_pointer_file(self) -> None:
        self._write_plan_toml("my-plan.toml")
        self.opsx_plan.write_active_plan(self.repo, "my-plan.toml")
        os.environ.pop("OPSX_PLAN", None)
        result = self.opsx_plan.planref.resolve_plan(self.repo, None)
        self.assertEqual(result, "my-plan.toml")

    def test_resolve_plan_raises_when_nothing_set(self) -> None:
        os.environ.pop("OPSX_PLAN", None)
        with self.assertRaises(self.opsx_plan.base.PlanError) as ctx:
            self.opsx_plan.planref.resolve_plan(self.repo, None)
        self.assertIn("no plan specified", str(ctx.exception).lower())

    def test_resolve_plan_raises_on_stale_pointer(self) -> None:
        self.opsx_plan.write_active_plan(self.repo, "deleted-plan.toml")
        os.environ.pop("OPSX_PLAN", None)
        with self.assertRaises(self.opsx_plan.base.PlanError) as ctx:
            self.opsx_plan.planref.resolve_plan(self.repo, None)
        self.assertIn("active plan pointer references missing file", str(ctx.exception))
        self.assertIn("deleted-plan.toml", str(ctx.exception))
        self.assertIn("opsx-plan use", str(ctx.exception))

    # ── 5.2: explicit and OPSX_PLAN override the pointer ─────────────────

    def test_resolve_plan_explicit_overrides_pointer(self) -> None:
        self._write_plan_toml("my-plan.toml")
        self.opsx_plan.write_active_plan(self.repo, "my-plan.toml")
        os.environ.pop("OPSX_PLAN", None)
        result = self.opsx_plan.planref.resolve_plan(self.repo, "override.toml")
        self.assertEqual(result, "override.toml")

    def test_resolve_plan_env_overrides_pointer(self) -> None:
        self._write_plan_toml("my-plan.toml")
        self.opsx_plan.write_active_plan(self.repo, "my-plan.toml")
        os.environ["OPSX_PLAN"] = "env-plan.toml"
        result = self.opsx_plan.planref.resolve_plan(self.repo, None)
        self.assertEqual(result, "env-plan.toml")

    # ── 5.3: use, compile activation, run activation, status, stale ─────

    def test_cmd_use_writes_pointer(self) -> None:
        plan = self._write_plan_toml("my-plan.toml")
        args = argparse.Namespace(repo=str(self.repo), plan="my-plan.toml")
        rc = self.opsx_plan.cmd_use(args)
        self.assertEqual(rc, 0)
        pointer = self.opsx_plan.planref.read_active_plan(self.repo)
        self.assertEqual(pointer, "my-plan.toml")

    def test_cmd_use_rejects_nonexistent_plan(self) -> None:
        args = argparse.Namespace(repo=str(self.repo), plan="missing.toml")
        rc = self.opsx_plan.cmd_use(args)
        self.assertEqual(rc, 2)
        self.assertIsNone(self.opsx_plan.planref.read_active_plan(self.repo))

    def test_cmd_use_rejects_invalid_toml(self) -> None:
        p = self.repo / "bad.toml"
        p.write_text("not valid toml {{{", encoding="utf-8")
        args = argparse.Namespace(repo=str(self.repo), plan="bad.toml")
        rc = self.opsx_plan.cmd_use(args)
        self.assertEqual(rc, 2)
        self.assertIsNone(self.opsx_plan.planref.read_active_plan(self.repo))

    def test_cmd_use_rejects_plan_outside_repo(self) -> None:
        # Create a plan outside the repo
        outside = Path(tempfile.gettempdir()) / "outside-plan.toml"
        outside.write_text(
            '[plan]\nname = "outside"\nadapter = "opencode"\n\n'
            "[[changes]]\nid = \"oc\"\n",
            encoding="utf-8",
        )
        try:
            args = argparse.Namespace(repo=str(self.repo), plan=str(outside))
            rc = self.opsx_plan.cmd_use(args)
            self.assertEqual(rc, 2)
            self.assertIsNone(self.opsx_plan.planref.read_active_plan(self.repo))
        finally:
            outside.unlink(missing_ok=True)

    def test_compile_auto_activates_output_plan(self) -> None:
        source = self._write_plan_md("plan.md")
        out = self.repo / "out.toml"
        os.environ["OPSX_CONTROLLER_MODEL"] = "test-provider/test-model"

        valid_toml = (
            '[plan]\nname = "test"\nadapter = "opencode"\n\n'
            "[[changes]]\nid = \"c1\"\nphase = 1\n"
        )
        original = self.opsx_plan.compiler.run_compile_client
        try:
            self.opsx_plan.compiler.run_compile_client = lambda repo, adapter, model, prompt: (valid_toml, "")
            args = argparse.Namespace(repo=str(self.repo), source="plan.md",
                                      output=str(out), force=False)
            rc = self.opsx_plan.cmd_compile(args)
            self.assertEqual(rc, 0)
            pointer = self.opsx_plan.planref.read_active_plan(self.repo)
            self.assertIsNotNone(pointer)
            self.assertEqual(Path(pointer), Path("out.toml"))
        finally:
            self.opsx_plan.compiler.run_compile_client = original

    def test_run_explicit_auto_activates_after_successful_load(self) -> None:
        plan = self._write_plan_toml("my-plan.toml")
        # Ensure the plan's referenced change is authored so cmd_run can
        # process it without hitting the create stage.
        cdir = self.repo / "openspec" / "changes" / "test-change"
        cdir.mkdir(parents=True)
        (cdir / "proposal.md").write_text("## Why\n", encoding="utf-8")
        (cdir / "tasks.md").write_text("- [ ] 1.1 task\n", encoding="utf-8")
        os.environ["OPSX_CONTROLLER_MODEL"] = "test-model"

        def fake_run_direct_change(repo, cfg, state, cid, budget_deadline=None, budget_usd=0.0):
            return self.opsx_plan.base.DONE

        original = self.opsx_plan.run_direct_change
        try:
            self.opsx_plan.run_direct_change = fake_run_direct_change
            args = argparse.Namespace(
                repo=str(self.repo), plan=str(plan),
                dry_run=False, budget_minutes=0, max_changes=0,
                only=[], create_only=False,
            )
            rc = self.opsx_plan.cmd_run(args)
            # cmd_run returns 0 on success
            self.assertEqual(rc, 0)
            pointer = self.opsx_plan.planref.read_active_plan(self.repo)
            self.assertEqual(pointer, "my-plan.toml")
        finally:
            self.opsx_plan.run_direct_change = original

    def test_run_preserves_manifest_skip_keys_without_cli_flags(self) -> None:
        """`--skip-warning` defaults to False and must not clobber a manifest.

        The flags are additive overrides; a plan that opts into skipping keeps
        that setting when the operator does not repeat it on the command line.
        """
        plan = self._write_plan_toml(
            "skip-plan.toml",
            '[plan]\nname = "skip-plan"\nadapter = "opencode"\n'
            "require_clean_tracked = false\n"
            "skip_warning = true\n\n"
            '[[changes]]\nid = "test-change"\n',
        )
        cdir = self.repo / "openspec" / "changes" / "test-change"
        cdir.mkdir(parents=True)
        (cdir / "proposal.md").write_text("## Why\n", encoding="utf-8")
        (cdir / "tasks.md").write_text("- [ ] 1.1 task\n", encoding="utf-8")
        os.environ["OPSX_CONTROLLER_MODEL"] = "test-model"

        seen: dict = {}

        def fake_run_direct_change(repo, cfg, state, cid, budget_deadline=None, budget_usd=0.0):
            seen.update(cfg)
            return self.opsx_plan.base.DONE

        original = self.opsx_plan.run_direct_change
        try:
            self.opsx_plan.run_direct_change = fake_run_direct_change
            args = argparse.Namespace(
                repo=str(self.repo), plan=str(plan),
                dry_run=False, budget_minutes=0, max_changes=0,
                only=[], create_only=False,
            )
            self.assertEqual(self.opsx_plan.cmd_run(args), 0)
        finally:
            self.opsx_plan.run_direct_change = original

        self.assertTrue(
            seen.get("skip_warning"),
            "manifest skip_warning must survive an absent --skip-warning flag",
        )
        self.assertFalse(seen.get("skip_suggestion"))

    def test_run_explicit_failed_load_does_not_activate(self) -> None:
        """When load_plan fails for an explicit plan, the active-plan pointer
        must not be written (bug fix verification)."""
        p = self.repo / "bad-plan.toml"
        p.write_text("this is not valid toml", encoding="utf-8")

        args = argparse.Namespace(
            repo=str(self.repo), plan=str(p),
            dry_run=False, budget_minutes=0, max_changes=0,
            only=[], create_only=False,
        )

        # Ensure no pointer exists before the call
        self.assertIsNone(self.opsx_plan.planref.read_active_plan(self.repo))

        with self.assertRaises(self.opsx_plan.base.PlanError):
            self.opsx_plan.cmd_run(args)

        # Pointer must still be absent
        self.assertIsNone(self.opsx_plan.planref.read_active_plan(self.repo))

    def test_status_shows_active_plan_in_header(self) -> None:
        plan = self._write_plan_toml("my-plan.toml")
        self.opsx_plan.write_active_plan(self.repo, "my-plan.toml")

        stdout = io.StringIO()

        def fake_run_direct_change(repo, cfg, state, cid, budget_deadline=None, budget_usd=0.0):
            return self.opsx_plan.base.DONE

        original = self.opsx_plan.run_direct_change
        try:
            self.opsx_plan.run_direct_change = fake_run_direct_change
            with mock.patch("sys.stdout", stdout):
                args = argparse.Namespace(repo=str(self.repo), plan=str(plan))
                rc = self.opsx_plan.cmd_status(args)
                output = stdout.getvalue()
                self.assertIn("(active: my-plan.toml)", output)
        finally:
            self.opsx_plan.run_direct_change = original

    def test_status_shows_inspected_plan_when_differs_from_active(self) -> None:
        active_plan = self._write_plan_toml("active.toml")
        other_plan = self._write_plan_toml("other.toml")
        self.opsx_plan.write_active_plan(self.repo, "active.toml")

        stdout = io.StringIO()

        def fake_run_direct_change(repo, cfg, state, cid, budget_deadline=None, budget_usd=0.0):
            return self.opsx_plan.base.DONE

        original = self.opsx_plan.run_direct_change
        try:
            self.opsx_plan.run_direct_change = fake_run_direct_change
            with mock.patch("sys.stdout", stdout):
                args = argparse.Namespace(repo=str(self.repo), plan=str(other_plan))
                rc = self.opsx_plan.cmd_status(args)
                output = stdout.getvalue()
                self.assertIn("(active: active.toml)", output)
                self.assertIn("[inspected:", output)
        finally:
            self.opsx_plan.run_direct_change = original

    def test_resolve_plan_stale_pointer_includes_opsx_plan_use_hint(self) -> None:
        """A stale pointer error must tell the user to run 'opsx-plan use'."""
        self.opsx_plan.write_active_plan(self.repo, "deleted.toml")
        os.environ.pop("OPSX_PLAN", None)
        with self.assertRaises(self.opsx_plan.base.PlanError) as ctx:
            self.opsx_plan.planref.resolve_plan(self.repo, None)
        self.assertIn("opsx-plan use", str(ctx.exception))

    def test_write_and_read_active_plan_roundtrip(self) -> None:
        self.assertIsNone(self.opsx_plan.planref.read_active_plan(self.repo))
        self.opsx_plan.write_active_plan(self.repo, "some/plan.toml")
        self.assertEqual(self.opsx_plan.planref.read_active_plan(self.repo), "some/plan.toml")
        # Overwrite
        self.opsx_plan.write_active_plan(self.repo, "other.toml")
        self.assertEqual(self.opsx_plan.planref.read_active_plan(self.repo), "other.toml")

    def test_read_active_plan_returns_none_for_empty_file(self) -> None:
        pp = self.opsx_plan.planref.active_plan_pointer_path(self.repo)
        pp.parent.mkdir(parents=True, exist_ok=True)
        pp.write_text("   \n", encoding="utf-8")
        self.assertIsNone(self.opsx_plan.planref.read_active_plan(self.repo))

    def test_validate_active_plan_succeeds_for_valid_plan(self) -> None:
        self._write_plan_toml("my-plan.toml")
        result = self.opsx_plan.validate_active_plan(self.repo, "my-plan.toml")
        self.assertTrue(result.is_file())

    def test_validate_active_plan_raises_for_missing_file(self) -> None:
        with self.assertRaises(self.opsx_plan.base.PlanError) as ctx:
            self.opsx_plan.validate_active_plan(self.repo, "missing.toml")
        self.assertIn("does not exist", str(ctx.exception))
        self.assertIn("missing.toml", str(ctx.exception))

    # ── 5.4: resolve_plan_path resolves relative paths against repo ─────

    def test_resolve_plan_path_relative_resolves_against_repo(self) -> None:
        self._write_plan_toml("rel/plan.toml")
        result = self.opsx_plan.planref._resolve_plan_path(self.repo, "rel/plan.toml")
        expected = (self.repo / "rel" / "plan.toml").resolve()
        self.assertEqual(result, expected)

    def test_resolve_plan_path_absolute_stays_absolute(self) -> None:
        self._write_plan_toml("some/plan.toml")
        abs_path = str((self.repo / "some" / "plan.toml").resolve())
        result = self.opsx_plan.planref._resolve_plan_path(self.repo, abs_path)
        self.assertEqual(result, Path(abs_path).resolve())

    # ── 5.5: status shows inspected path from OPSX_PLAN ─────────────────

    def test_status_shows_inspected_plan_via_opsx_plan_when_differs(self) -> None:
        plan_active = self._write_plan_toml("active.toml")
        plan_env = self._write_plan_toml("env-plan.toml")
        self.opsx_plan.write_active_plan(self.repo, "active.toml")
        os.environ["OPSX_PLAN"] = "env-plan.toml"

        stdout = io.StringIO()

        def fake_run_direct_change(repo, cfg, state, cid, budget_deadline=None, budget_usd=0.0):
            return self.opsx_plan.base.DONE

        original = self.opsx_plan.run_direct_change
        try:
            self.opsx_plan.run_direct_change = fake_run_direct_change
            with mock.patch("sys.stdout", stdout):
                args = argparse.Namespace(repo=str(self.repo), plan=None)
                rc = self.opsx_plan.cmd_status(args)
                output = stdout.getvalue()
                self.assertIn("(active: active.toml)", output)
                self.assertIn("[inspected: env-plan.toml]", output)
        finally:
            self.opsx_plan.run_direct_change = original

    def test_status_no_inspected_note_when_opsx_plan_matches_active(self) -> None:
        self._write_plan_toml("same.toml")
        self.opsx_plan.write_active_plan(self.repo, "same.toml")
        os.environ["OPSX_PLAN"] = "same.toml"

        stdout = io.StringIO()

        def fake_run_direct_change(repo, cfg, state, cid, budget_deadline=None, budget_usd=0.0):
            return self.opsx_plan.base.DONE

        original = self.opsx_plan.run_direct_change
        try:
            self.opsx_plan.run_direct_change = fake_run_direct_change
            with mock.patch("sys.stdout", stdout):
                args = argparse.Namespace(repo=str(self.repo), plan=None)
                rc = self.opsx_plan.cmd_status(args)
                output = stdout.getvalue()
                self.assertIn("(active: same.toml)", output)
                self.assertNotIn("[inspected:", output)
        finally:
            self.opsx_plan.run_direct_change = original

    # ── 5.6: command-level --repo plan-resolution coverage ─────────────

    def test_cmd_approve_resolves_via_active_pointer(self) -> None:
        """cmd_approve with plan=None resolves the plan via the active pointer."""
        self._write_plan_toml("my-plan.toml")
        self.opsx_plan.write_active_plan(self.repo, "my-plan.toml")

        args = argparse.Namespace(
            repo=str(self.repo), plan=None, change=["test-change"],
            approve_all=False,
        )
        rc = self.opsx_plan.cmd_approve(args)

        self.assertEqual(rc, 0)
        state = self.opsx_plan.state_mod.load_state(self.repo, "test-plan")
        self.assertIn("test-change", state["approvals"])

    def test_cmd_accept_resolves_via_active_pointer(self) -> None:
        """cmd_accept with plan=None resolves the plan via the active pointer."""
        self._write_plan_toml(
            "my-plan.toml",
            '[plan]\nname = "test-plan"\nadapter = "opencode"\n'
            'created_check = ""\n\n'
            "[[changes]]\nid = \"test-change\"\n",
        )
        self.opsx_plan.write_active_plan(self.repo, "my-plan.toml")
        cdir = self.repo / "openspec" / "changes" / "test-change"
        cdir.mkdir(parents=True)
        (cdir / "proposal.md").write_text("## Why\n", encoding="utf-8")
        (cdir / "tasks.md").write_text("- [ ] 1.1 task\n", encoding="utf-8")

        args = argparse.Namespace(
            repo=str(self.repo), plan=None, change=["test-change"],
            accept_all=False,
        )
        rc = self.opsx_plan.cmd_accept(args)

        self.assertEqual(rc, 0)
        state = self.opsx_plan.state_mod.load_state(self.repo, "test-plan")
        self.assertTrue(state["changes"]["test-change"]["accepted"])

    def test_cmd_accept_phase_persists_successes_before_invalid_change(self) -> None:
        """Phase-wide accept must persist valid accepts even if one change fails."""
        self._write_plan_toml(
            "my-plan.toml",
            '[plan]\nname = "test-plan"\nadapter = "opencode"\n'
            'created_check = ""\n\n'
            '[[changes]]\nid = "add-cost-budget-run-flag"\nphase = 3\n'
            '[[changes]]\nid = "batch-gate-and-reset-commands"\nphase = 3\n'
            '[[changes]]\nid = "add-plan-logs-command"\nphase = 3\n'
            '[[changes]]\nid = "add-run-event-notifications"\nphase = 3\n',
        )
        self.opsx_plan.write_active_plan(self.repo, "my-plan.toml")
        for cid in (
            "add-cost-budget-run-flag",
            "batch-gate-and-reset-commands",
            "add-plan-logs-command",
        ):
            cdir = self.repo / "openspec" / "changes" / cid
            cdir.mkdir(parents=True)
            (cdir / "proposal.md").write_text("## Why\n", encoding="utf-8")
            (cdir / "tasks.md").write_text("- [ ] 1.1 task\n", encoding="utf-8")

        stderr = io.StringIO()
        args = argparse.Namespace(repo=str(self.repo), plan=None, change=["P3"],
                                   accept_all=False)
        with mock.patch("sys.stderr", stderr):
            rc = self.opsx_plan.cmd_accept(args)

        self.assertEqual(rc, 2)
        self.assertIn(
            "refusing to accept add-run-event-notifications",
            stderr.getvalue(),
        )
        state = self.opsx_plan.state_mod.load_state(self.repo, "test-plan")
        for cid in (
            "add-cost-budget-run-flag",
            "batch-gate-and-reset-commands",
            "add-plan-logs-command",
        ):
            self.assertTrue(state["changes"][cid]["accepted"])
        self.assertNotIn("add-run-event-notifications", state["changes"])

    def test_cmd_reset_resolves_via_active_pointer(self) -> None:
        """cmd_reset with plan=None resolves the plan via the active pointer."""
        self._write_plan_toml("my-plan.toml")
        self.opsx_plan.write_active_plan(self.repo, "my-plan.toml")

        args = argparse.Namespace(
            repo=str(self.repo), plan=None, change=["test-change"],
            failed=False,
        )
        rc = self.opsx_plan.cmd_reset(args)

        self.assertEqual(rc, 0)
        state = self.opsx_plan.state_mod.load_state(self.repo, "test-plan")
        self.assertEqual(state["changes"]["test-change"]["status"], "pending")

    def test_cmd_run_resolves_via_active_pointer(self) -> None:
        """cmd_run with plan=None resolves the plan via the active pointer and
        runs the change successfully."""
        self._write_plan_toml("my-plan.toml")
        self.opsx_plan.write_active_plan(self.repo, "my-plan.toml")
        cdir = self.repo / "openspec" / "changes" / "test-change"
        cdir.mkdir(parents=True)
        (cdir / "proposal.md").write_text("## Why\n", encoding="utf-8")
        (cdir / "tasks.md").write_text("- [ ] 1.1 task\n", encoding="utf-8")
        os.environ["OPSX_CONTROLLER_MODEL"] = "test-model"

        def fake_run_direct_change(repo, cfg, state, cid, budget_deadline=None, budget_usd=0.0):
            return self.opsx_plan.base.DONE

        original = self.opsx_plan.run_direct_change
        try:
            self.opsx_plan.run_direct_change = fake_run_direct_change
            args = argparse.Namespace(
                repo=str(self.repo), plan=None,
                dry_run=False, budget_minutes=0, max_changes=0,
                only=[], create_only=False,
            )
            rc = self.opsx_plan.cmd_run(args)
            self.assertEqual(rc, 0)
        finally:
            self.opsx_plan.run_direct_change = original

    def test_cmd_report_resolves_via_active_pointer(self) -> None:
        """cmd_report with plan=None resolves the plan via the active pointer."""
        plan_name = "test-plan"
        plan = self._write_plan_toml("my-plan.toml")
        self.opsx_plan.write_active_plan(self.repo, "my-plan.toml")

        # Minimal telemetry and state so the report can produce output.
        tele_dir = self.repo / ".opsx-plan" / "telemetry"
        tele_dir.mkdir(parents=True, exist_ok=True)
        record = {
            "schema_version": 1,
            "uid": "uid-001",
            "plan_name": plan_name,
            "run_id": "run-001",
            "change_id": "test-change",
            "stage": "implement",
            "round": 1,
            "status": "completed",
            "started_at": "2026-07-01T10:00:00",
            "ended_at": "2026-07-01T10:02:00",
            "duration_ms": 120000,
            "usage": {
                "usage_available": True,
                "input_tokens": 10000,
                "output_tokens": 2000,
                "cached_input_tokens": None,
                "reasoning_tokens": None,
                "total_tokens": 12000,
                "usage_source": "worker_json",
            },
            "cost": {
                "status": "estimated",
                "estimated_cost": 0.05,
                "pricing_catalog_version": None,
                "price_snapshot": None,
                "unresolved_reason": None,
            },
            "model": {
                "provider": "openai",
                "model_id": "gpt-4o",
                "model_alias": None,
            },
            "result": {
                "stage_status": "completed",
                "verdict": None,
                "critical_count": 0,
                "warning_count": 0,
                "note_count": 0,
            },
        }
        (tele_dir / f"{plan_name}.jsonl").write_text(
            json.dumps(record) + "\n", encoding="utf-8",
        )
        state_dir = self.repo / ".opsx-plan"
        state_dir.mkdir(parents=True, exist_ok=True)
        (state_dir / f"{plan_name}.state.json").write_text(
            json.dumps({"plan": plan_name, "approvals": [], "changes": {
                "test-change": {"status": "done", "round": 1, "phase": "done"},
            }}), encoding="utf-8",
        )

        stdout = io.StringIO()
        stderr = io.StringIO()
        args = argparse.Namespace(
            repo=str(self.repo), plan=None, json=False,
            change=None, run_id=None, stage=None, model=None,
        )
        with mock.patch("sys.stdout", stdout), mock.patch("sys.stderr", stderr):
            rc = self.opsx_plan.report.cmd_report(args)
        self.assertEqual(rc, 0)
        self.assertIn("test-plan", stdout.getvalue())

    def test_cmd_dashboard_resolves_via_active_pointer(self) -> None:
        """cmd_dashboard with plan=None resolves the plan via the active pointer."""
        plan_name = "test-plan"
        self._write_plan_toml("my-plan.toml")
        self.opsx_plan.write_active_plan(self.repo, "my-plan.toml")

        # Minimal telemetry and state so the dashboard can render.
        tele_dir = self.repo / ".opsx-plan" / "telemetry"
        tele_dir.mkdir(parents=True, exist_ok=True)
        record = {
            "schema_version": 1,
            "uid": "uid-001",
            "plan_name": plan_name,
            "run_id": "run-001",
            "change_id": "test-change",
            "stage": "implement",
            "round": 1,
            "status": "completed",
            "started_at": "2026-07-01T10:00:00",
            "ended_at": "2026-07-01T10:02:00",
            "duration_ms": 120000,
            "usage": {
                "usage_available": True,
                "input_tokens": 10000,
                "output_tokens": 2000,
                "cached_input_tokens": None,
                "reasoning_tokens": None,
                "total_tokens": 12000,
                "usage_source": "worker_json",
            },
            "cost": {
                "status": "estimated",
                "estimated_cost": 0.05,
                "pricing_catalog_version": None,
                "price_snapshot": None,
                "unresolved_reason": None,
            },
            "model": {
                "provider": "openai",
                "model_id": "gpt-4o",
                "model_alias": None,
            },
            "result": {
                "stage_status": "completed",
                "verdict": None,
                "critical_count": 0,
                "warning_count": 0,
                "note_count": 0,
            },
        }
        (tele_dir / f"{plan_name}.jsonl").write_text(
            json.dumps(record) + "\n", encoding="utf-8",
        )
        state_dir = self.repo / ".opsx-plan"
        state_dir.mkdir(parents=True, exist_ok=True)
        (state_dir / f"{plan_name}.state.json").write_text(
            json.dumps({"plan": plan_name, "approvals": [], "changes": {
                "test-change": {"status": "done", "round": 1, "phase": "done"},
            }}), encoding="utf-8",
        )

        stdout = io.StringIO()
        args = argparse.Namespace(
            repo=str(self.repo), plan=None,
            output=None, run_id=None, change=None,
        )
        with mock.patch("sys.stdout", stdout):
            rc = self.opsx_plan.dashboard.cmd_dashboard(args)
        self.assertEqual(rc, 0)
        self.assertIn("Dashboard written to:", stdout.getvalue())


class SpendBudgetTests(unittest.TestCase):
    """Tests for spend-budget enforcement and reporting (--budget-usd)."""

    def setUp(self) -> None:
        self.opsx_plan = load_opsx_plan()
        self.tmp = tempfile.TemporaryDirectory()
        self.repo = Path(self.tmp.name)
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
        self.cid = "add-budget-test"
        self.plan_name = f"run-{self.cid}"
        self.cfg = {
            "name": self.plan_name,
            "adapter": "opencode",
            "implement_invoke": "opencode run --agent opsx-implementer",
            "review_invoke": "opencode run --agent opsx-reviewer",
            "archive_invoke": "opencode run --agent opsx-archiver",
            "state_file": ".opencode/opsx-controller/{change}.json",
            "timeout_minutes": 1,
            "max_rounds": 5,
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
        self._saved_checks = self.opsx_plan.groundtruth.run_fast_checks

    def tearDown(self) -> None:
        self.opsx_plan.invoke_direct_stage = self._saved_invoke
        self.opsx_plan.groundtruth.run_fast_checks = self._saved_checks
        self.tmp.cleanup()

    def write_authored_change(self, cid: str) -> None:
        cdir = self.repo / "openspec" / "changes" / cid
        cdir.mkdir(parents=True)
        (cdir / "proposal.md").write_text("## Why\n", encoding="utf-8")
        (cdir / "tasks.md").write_text(
            "## 1. Tasks\n\n- [ ] 1.1 Example task\n- [ ] 1.2 Example task\n",
            encoding="utf-8",
        )

    def _write_telemetry(self, run_id: str, records: list[dict]) -> None:
        """Write telemetry records to the plan's JSONL file."""
        telemetry_dir = self.repo / ".opsx-plan" / "telemetry"
        telemetry_dir.mkdir(parents=True, exist_ok=True)
        jsonl = telemetry_dir / f"{self.plan_name}.jsonl"
        with open(jsonl, "a", encoding="utf-8") as fh:
            for rec in records:
                fh.write(json.dumps(rec) + "\n")

    def _telemetry_rec(
        self,
        run_id: str,
        stage: str,
        cost_status: str,
        estimated_cost: float | None,
    ) -> dict:
        return {
            "schema_version": self.opsx_plan.telemetry.TELEMETRY_SCHEMA_VERSION,
            "uid": str(uuid.uuid4()),
            "plan_name": self.plan_name,
            "run_id": run_id,
            "change_id": self.cid,
            "stage": stage,
            "round": 1,
            "cost": {
                "status": cost_status,
                "estimated_cost": estimated_cost,
            },
        }

    # -- compute_run_spend unit tests ----------------------------------------

    def test_compute_run_spend_no_telemetry_file(self) -> None:
        spend = self.opsx_plan.compute_run_spend(self.repo, self.plan_name, "any-run")
        self.assertEqual(spend["cumulative_spend"], 0.0)
        self.assertEqual(spend["resolved_stages"], 0)
        self.assertEqual(spend["unresolved_stages"], 0)

    def test_compute_run_spend_filters_by_run_id(self) -> None:
        run_a = "run-aaa"
        run_b = "run-bbb"
        self._write_telemetry(
            run_a,
            [
                self._telemetry_rec(run_a, "implement", "estimated", 0.50),
                self._telemetry_rec(run_a, "review", "estimated", 0.25),
            ],
        )
        self._write_telemetry(
            run_b,
            [
                self._telemetry_rec(run_b, "implement", "estimated", 10.00),
            ],
        )

        spend = self.opsx_plan.compute_run_spend(self.repo, self.plan_name, run_a)
        self.assertEqual(spend["cumulative_spend"], 0.75)
        self.assertEqual(spend["resolved_stages"], 2)
        self.assertEqual(spend["unresolved_stages"], 0)

    def test_compute_run_spend_counts_unresolved_and_unavailable(self) -> None:
        run_id = "run-mixed"
        self._write_telemetry(
            run_id,
            [
                self._telemetry_rec(run_id, "implement", "estimated", 0.40),
                self._telemetry_rec(run_id, "review", "unresolved", None),
                self._telemetry_rec(run_id, "implement", "estimated", 0.35),
                self._telemetry_rec(run_id, "review", "unavailable", None),
                self._telemetry_rec(run_id, "archive", "unresolved", None),
            ],
        )

        spend = self.opsx_plan.compute_run_spend(self.repo, self.plan_name, run_id)
        self.assertEqual(spend["cumulative_spend"], 0.75)
        self.assertEqual(spend["resolved_stages"], 2)
        self.assertEqual(spend["unresolved_stages"], 3)

    def test_compute_run_spend_estimated_without_cost_counts_as_unresolved(self) -> None:
        run_id = "run-null-estimate"
        self._write_telemetry(
            run_id,
            [
                self._telemetry_rec(run_id, "implement", "estimated", None),
            ],
        )

        spend = self.opsx_plan.compute_run_spend(self.repo, self.plan_name, run_id)
        self.assertEqual(spend["cumulative_spend"], 0.0)
        self.assertEqual(spend["resolved_stages"], 0)
        self.assertEqual(spend["unresolved_stages"], 1)

    # -- budget enforcement integration tests --------------------------------

    def test_run_stops_after_reaching_spend_cap(self) -> None:
        """4.1: A run with --budget-usd stops dispatching after cumulative
        estimated spend reaches the cap, and leaves state resumable."""
        self.write_authored_change(self.cid)
        record = self.opsx_plan.state_mod.rec(self.state, self.cid)
        record["max_rounds"] = self.cfg["max_rounds"]
        record["tracked_change_files"] = self.opsx_plan.state_mod.change_context_paths(
            self.repo, self.cid
        )

        # Seed telemetry that already has partial spend within the budget
        run_id = "run-budget-001"
        self.state["run_id"] = run_id
        self._write_telemetry(
            run_id,
            [
                self._telemetry_rec(run_id, "implement", "estimated", 0.70),
            ],
        )

        calls: list[str] = []

        def fake_invoke(repo, cfg, cid, stage, round_num, input_block):
            calls.append(stage)
            log_path = self.opsx_plan.next_stage_log_path(repo, cid, stage, round_num)
            log_path.parent.mkdir(parents=True, exist_ok=True)
            payload = {
                "status": "implemented",
                "change": cid,
                "round": round_num,
                "progress_made": True,
                "completed_tasks": ["1.1"],
                "remaining_tasks": ["1.2"],
                "task_counts": {"complete": 1, "total": 2},
                "files_touched": [],
                "known_change_files": [],
                "summary": "implemented round",
            }
            log_path.write_text(json.dumps(payload) + "\n", encoding="utf-8")
            return "exited", log_path

        self.opsx_plan.invoke_direct_stage = fake_invoke

        # Mock compute_run_spend to return below cap on first call (pre-dispatch)
        # and above cap on second call (post-implement dispatch check)
        original_compute = self.opsx_plan.compute_run_spend

        spend_values = [
            {"cumulative_spend": 0.70, "resolved_stages": 1, "unresolved_stages": 0},
            {"cumulative_spend": 1.05, "resolved_stages": 2, "unresolved_stages": 0},
        ]
        call_count = [0]

        def mock_compute(repo, plan_name, rid):
            idx = min(call_count[0], len(spend_values) - 1)
            val = spend_values[idx]
            call_count[0] += 1
            return val

        self.opsx_plan.compute_run_spend = mock_compute

        try:
            result = self.opsx_plan.run_direct_change(
                self.repo, self.cfg, self.state, self.cid, budget_usd=1.00
            )
        finally:
            self.opsx_plan.compute_run_spend = original_compute

        # First dispatch: spend 0.70 < 1.00 → implement runs.
        # After implement, loop checks again: spend 1.05 >= 1.00 → stop.
        self.assertEqual(result, "budget")
        record = self.opsx_plan.state_mod.rec(self.state, self.cid)
        self.assertEqual(record["last_result"], "spend_budget_exhausted")
        # Status must be PENDING (resumable), not FAILED
        self.assertEqual(record["status"], self.opsx_plan.base.PENDING)
        self.assertIn("spend budget exhausted", record["reason"])
        self.assertIn("resolved", record["reason"])
        self.assertIn("unresolved", record["reason"])

    def test_run_completes_without_reaching_spend_cap(self) -> None:
        """4.2: A run with a high --budget-usd completes normally."""
        self.write_authored_change(self.cid)
        record = self.opsx_plan.state_mod.rec(self.state, self.cid)
        record["max_rounds"] = self.cfg["max_rounds"]
        record["tracked_change_files"] = self.opsx_plan.state_mod.change_context_paths(
            self.repo, self.cid
        )

        # Seed telemetry with low spend
        run_id = "run-budget-002"
        self.state["run_id"] = run_id
        self._write_telemetry(
            run_id,
            [
                self._telemetry_rec(run_id, "implement", "estimated", 0.20),
            ],
        )

        def fake_invoke(repo, cfg, cid, stage, round_num, input_block):
            log_path = self.opsx_plan.next_stage_log_path(repo, cid, stage, round_num)
            log_path.parent.mkdir(parents=True, exist_ok=True)
            if stage == "implement":
                payload = {
                    "status": "implemented",
                    "change": cid,
                    "round": round_num,
                    "progress_made": True,
                    "completed_tasks": ["1.1", "1.2"],
                    "remaining_tasks": [],
                    "task_counts": {"complete": 2, "total": 2},
                    "files_touched": [],
                    "known_change_files": [],
                    "summary": "implemented",
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
            else:  # archive
                src = self.repo / "openspec" / "changes" / self.cid
                archive_rel = f"openspec/changes/archive/2026-07-10-{self.cid}"
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
                if staged:
                    git(
                        self.repo,
                        "-c", "user.email=test@example.invalid",
                        "-c", "user.name=Test User",
                        "commit", "-m", f"archive({self.cid}): archive completed OpenSpec change",
                    )
                    commit = subprocess.run(
                        ["git", "rev-parse", "HEAD"], cwd=self.repo,
                        check=True, capture_output=True, text=True,
                    ).stdout.strip()
                else:
                    commit = ""
                payload = {
                    "status": "archived",
                    "change": cid,
                    "archive_path": archive_rel,
                    "spec_sync_status": "no-delta",
                    "commit": commit,
                    "summary": "archive succeeded",
                }
            log_path.write_text(json.dumps(payload) + "\n", encoding="utf-8")
            return "exited", log_path

        self.opsx_plan.invoke_direct_stage = fake_invoke

        # Mock compute_run_spend to always return low spend
        original_compute = self.opsx_plan.compute_run_spend

        def mock_compute(repo, plan_name, rid):
            return {"cumulative_spend": 0.20, "resolved_stages": 1, "unresolved_stages": 0}

        self.opsx_plan.compute_run_spend = mock_compute

        try:
            result = self.opsx_plan.run_direct_change(
                self.repo, self.cfg, self.state, self.cid, budget_usd=999.00
            )
        finally:
            self.opsx_plan.compute_run_spend = original_compute

        self.assertEqual(result, self.opsx_plan.base.DONE)
        record = self.opsx_plan.state_mod.rec(self.state, self.cid)
        self.assertEqual(record["status"], self.opsx_plan.base.DONE)

    def test_no_spend_check_when_budget_usd_is_zero(self) -> None:
        """budget_usd=0 (default/omitted) must not trigger spend checks."""
        self.write_authored_change(self.cid)
        record = self.opsx_plan.state_mod.rec(self.state, self.cid)
        record["max_rounds"] = self.cfg["max_rounds"]
        record["tracked_change_files"] = self.opsx_plan.state_mod.change_context_paths(
            self.repo, self.cid
        )

        run_id = "run-budget-003"
        self.state["run_id"] = run_id
        self._write_telemetry(
            run_id,
            [
                self._telemetry_rec(run_id, "implement", "estimated", 999.00),
            ],
        )

        def fake_invoke(repo, cfg, cid, stage, round_num, input_block):
            log_path = self.opsx_plan.next_stage_log_path(repo, cid, stage, round_num)
            log_path.parent.mkdir(parents=True, exist_ok=True)
            if stage == "implement":
                payload = {
                    "status": "implemented",
                    "change": cid,
                    "round": round_num,
                    "progress_made": True,
                    "completed_tasks": ["1.1", "1.2"],
                    "remaining_tasks": [],
                    "task_counts": {"complete": 2, "total": 2},
                    "files_touched": [],
                    "known_change_files": [],
                    "summary": "implemented all tasks",
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
            else:  # archive
                payload = {
                    "status": "archived",
                    "change": cid,
                    "archive_path": "",
                    "spec_sync_status": "no-delta",
                    "commit": "",
                    "summary": "archive skipped for test",
                }
            log_path.write_text(json.dumps(payload) + "\n", encoding="utf-8")
            return "exited", log_path

        self.opsx_plan.invoke_direct_stage = fake_invoke

        # Mock compute_run_spend to verify it is NOT called
        call_count = [0]
        original_compute = self.opsx_plan.compute_run_spend

        def mock_compute(repo, plan_name, rid):
            call_count[0] += 1
            return {"cumulative_spend": 999.00, "resolved_stages": 1, "unresolved_stages": 0}

        self.opsx_plan.compute_run_spend = mock_compute

        try:
            # budget_usd=0 means no spend cap — even 999.00 of spend
            # should not trigger a budget stop
            result = self.opsx_plan.run_direct_change(
                self.repo, self.cfg, self.state, self.cid, budget_usd=0.0
            )
        finally:
            self.opsx_plan.compute_run_spend = original_compute

        # compute_run_spend must NOT be called when budget_usd is 0
        self.assertEqual(call_count[0], 0,
                         f"compute_run_spend called {call_count[0]} times; expected 0")
        # Run should proceed past implement and review; archive returns "stop"
        # because there's no real archive directory (expected)
        self.assertIn(result, ("continue", "stop"))


class BatchGateAndResetCommandTests(unittest.TestCase):
    """Tests for batch gate and reset commands."""

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

    def _write_plan_toml(self, path: str, content: str) -> Path:
        p = self.repo / path
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(textwrap.dedent(content), encoding="utf-8")
        return p

    def _plan_with_gated_changes(self) -> Path:
        """Plan with two pause_before changes, one without."""
        return self._write_plan_toml(
            "openspec/plans/test.toml",
            """\
            [plan]
            name = "test-plan"
            adapter = "opencode"
            review_created = false

            [[changes]]
            id = "gated-a"
            phase = 1
            pause_before = true

            [[changes]]
            id = "gated-b"
            phase = 1
            pause_before = true

            [[changes]]
            id = "no-gate"
            phase = 2
            depends_on = ["gated-a", "gated-b"]
            """,
        )

    def _plan_with_created_changes(self) -> Path:
        """Plan with review_created and orchestrator-created changes."""
        plan = self._write_plan_toml(
            "openspec/plans/test.toml",
            """\
            [plan]
            name = "test-plan"
            adapter = "opencode"
            review_created = true
            created_check = ""

            [[changes]]
            id = "created-a"
            phase = 1
            create_invoke = "echo create"

            [[changes]]
            id = "created-b"
            phase = 1
            create_invoke = "echo create"
            """,
        )
        # Authored changes so acceptance can verify
        for cid in ("created-a", "created-b"):
            cdir = self.repo / "openspec" / "changes" / cid
            cdir.mkdir(parents=True)
            (cdir / "proposal.md").write_text("## Why\n", encoding="utf-8")
            (cdir / "tasks.md").write_text("## 1. Tasks\n", encoding="utf-8")
        return plan

    def _activate_plan(self, plan_rel: str) -> None:
        self.opsx_plan.write_active_plan(self.repo, plan_rel)

    def test_approve_all_approves_all_awaiting_approval_changes(self) -> None:
        plan = self._plan_with_gated_changes()
        self._activate_plan(str(plan.relative_to(self.repo)))

        stdout = io.StringIO()
        args = argparse.Namespace(
            repo=str(self.repo),
            plan=None,
            change=[],
            approve_all=True,
        )
        with mock.patch("sys.stdout", stdout):
            rc = self.opsx_plan.cmd_approve(args)
        self.assertEqual(rc, 0)
        self.assertIn("Approved: gated-a, gated-b", stdout.getvalue())

        state = self.opsx_plan.state_mod.load_state(self.repo, "test-plan")
        self.assertIn("gated-a", state["approvals"])
        self.assertIn("gated-b", state["approvals"])
        self.assertNotIn("no-gate", state["approvals"])

    def test_approve_all_reports_empty_set(self) -> None:
        plan = self._plan_with_gated_changes()
        self._activate_plan(str(plan.relative_to(self.repo)))
        # Approve first, then second call has nothing left
        args = argparse.Namespace(
            repo=str(self.repo),
            plan=None,
            change=[],
            approve_all=True,
        )
        self.opsx_plan.cmd_approve(args)
        # Second call — no changes left awaiting approval
        stdout = io.StringIO()
        with mock.patch("sys.stdout", stdout):
            rc = self.opsx_plan.cmd_approve(args)
        self.assertEqual(rc, 0)
        self.assertIn("No changes are currently awaiting approval.", stdout.getvalue())

    def test_approve_all_does_not_affect_already_approved(self) -> None:
        plan = self._plan_with_gated_changes()
        self._activate_plan(str(plan.relative_to(self.repo)))
        state = self.opsx_plan.state_mod.load_state(self.repo, "test-plan")
        state["approvals"].append("gated-a")
        self.opsx_plan.state_mod.save_state(self.repo, "test-plan", state)

        stdout = io.StringIO()
        args = argparse.Namespace(
            repo=str(self.repo),
            plan=None,
            change=[],
            approve_all=True,
        )
        with mock.patch("sys.stdout", stdout):
            rc = self.opsx_plan.cmd_approve(args)
        self.assertEqual(rc, 0)
        self.assertIn("Approved: gated-b", stdout.getvalue())

        state = self.opsx_plan.state_mod.load_state(self.repo, "test-plan")
        self.assertEqual(state["approvals"].count("gated-a"), 1)
        self.assertIn("gated-b", state["approvals"])

    def test_accept_all_accepts_awaiting_acceptance_changes(self) -> None:
        plan = self._plan_with_created_changes()
        self._activate_plan(str(plan.relative_to(self.repo)))
        state = self.opsx_plan.state_mod.load_state(self.repo, "test-plan")
        for cid in ("created-a", "created-b"):
            r = self.opsx_plan.state_mod.rec(state, cid)
            r["created_by_orchestrator"] = True
        self.opsx_plan.state_mod.save_state(self.repo, "test-plan", state)

        stdout = io.StringIO()
        args = argparse.Namespace(
            repo=str(self.repo),
            plan=None,
            change=[],
            accept_all=True,
        )
        with mock.patch("sys.stdout", stdout):
            rc = self.opsx_plan.cmd_accept(args)
        self.assertEqual(rc, 0)
        self.assertIn("Accepted: created-a, created-b", stdout.getvalue())

        state = self.opsx_plan.state_mod.load_state(self.repo, "test-plan")
        self.assertTrue(state["changes"]["created-a"]["accepted"])
        self.assertTrue(state["changes"]["created-b"]["accepted"])

    def test_accept_all_reports_empty_set(self) -> None:
        plan = self._plan_with_created_changes()
        self._activate_plan(str(plan.relative_to(self.repo)))
        # No changes created by orchestrator = nothing to accept
        stdout = io.StringIO()
        args = argparse.Namespace(
            repo=str(self.repo),
            plan=None,
            change=[],
            accept_all=True,
        )
        with mock.patch("sys.stdout", stdout):
            rc = self.opsx_plan.cmd_accept(args)
        self.assertEqual(rc, 0)
        self.assertIn("No changes are currently awaiting acceptance.", stdout.getvalue())

    def test_accept_all_skips_non_awaiting_changes(self) -> None:
        plan = self._plan_with_created_changes()
        self._activate_plan(str(plan.relative_to(self.repo)))
        state = self.opsx_plan.state_mod.load_state(self.repo, "test-plan")
        # Only make created-a awaiting acceptance
        r = self.opsx_plan.state_mod.rec(state, "created-a")
        r["created_by_orchestrator"] = True
        # created-b not created by orchestrator, so not awaiting acceptance
        self.opsx_plan.state_mod.save_state(self.repo, "test-plan", state)

        stdout = io.StringIO()
        args = argparse.Namespace(
            repo=str(self.repo),
            plan=None,
            change=[],
            accept_all=True,
        )
        with mock.patch("sys.stdout", stdout):
            rc = self.opsx_plan.cmd_accept(args)
        self.assertEqual(rc, 0)
        self.assertIn("Accepted: created-a", stdout.getvalue())

        state = self.opsx_plan.state_mod.load_state(self.repo, "test-plan")
        self.assertTrue(state["changes"]["created-a"]["accepted"])
        self.assertFalse(state["changes"]["created-b"]["accepted"])

    def test_reset_failed_resets_all_failed_changes(self) -> None:
        plan = self._plan_with_gated_changes()
        self._activate_plan(str(plan.relative_to(self.repo)))
        state = self.opsx_plan.state_mod.load_state(self.repo, "test-plan")
        self.opsx_plan.state_mod.set_status(state, "gated-a", self.opsx_plan.base.FAILED, "test failure")
        self.opsx_plan.state_mod.set_status(state, "gated-b", self.opsx_plan.base.FAILED, "another failure")
        # no-gate stays done
        self.opsx_plan.state_mod.set_status(state, "no-gate", self.opsx_plan.base.DONE, "already done")
        self.opsx_plan.state_mod.save_state(self.repo, "test-plan", state)

        stdout = io.StringIO()
        args = argparse.Namespace(
            repo=str(self.repo),
            plan=None,
            change=[],
            failed=True,
        )
        with mock.patch("sys.stdout", stdout):
            rc = self.opsx_plan.cmd_reset(args)
        self.assertEqual(rc, 0)
        self.assertIn("Reset: gated-a, gated-b", stdout.getvalue())

        state = self.opsx_plan.state_mod.load_state(self.repo, "test-plan")
        self.assertEqual(state["changes"]["gated-a"]["status"], self.opsx_plan.base.PENDING)
        self.assertEqual(state["changes"]["gated-b"]["status"], self.opsx_plan.base.PENDING)
        self.assertEqual(state["changes"]["no-gate"]["status"], self.opsx_plan.base.DONE)

    def test_reset_failed_reports_empty_set(self) -> None:
        plan = self._plan_with_gated_changes()
        self._activate_plan(str(plan.relative_to(self.repo)))
        # No failed changes
        stdout = io.StringIO()
        args = argparse.Namespace(
            repo=str(self.repo),
            plan=None,
            change=[],
            failed=True,
        )
        with mock.patch("sys.stdout", stdout):
            rc = self.opsx_plan.cmd_reset(args)
        self.assertEqual(rc, 0)
        self.assertIn("No failed changes to reset.", stdout.getvalue())

    def test_reset_failed_does_not_affect_non_failed_changes(self) -> None:
        plan = self._plan_with_gated_changes()
        self._activate_plan(str(plan.relative_to(self.repo)))
        state = self.opsx_plan.state_mod.load_state(self.repo, "test-plan")
        # Only gated-a failed, gated-b is awaiting approval
        self.opsx_plan.state_mod.set_status(state, "gated-a", self.opsx_plan.base.FAILED, "test failure")
        self.opsx_plan.state_mod.save_state(self.repo, "test-plan", state)

        stdout = io.StringIO()
        args = argparse.Namespace(
            repo=str(self.repo),
            plan=None,
            change=[],
            failed=True,
        )
        with mock.patch("sys.stdout", stdout):
            rc = self.opsx_plan.cmd_reset(args)
        self.assertEqual(rc, 0)
        self.assertIn("Reset: gated-a", stdout.getvalue())
        self.assertNotIn("gated-b", stdout.getvalue())

        state = self.opsx_plan.state_mod.load_state(self.repo, "test-plan")
        self.assertEqual(state["changes"]["gated-a"]["status"], self.opsx_plan.base.PENDING)
        # gated-b was awaiting_approval, its state shouldn't be fully replaced
        self.assertNotEqual(
            state["changes"]["gated-b"].get("reason", ""),
            "reset by operator",
        )

    def test_status_shows_approve_guidance_for_awaiting_approval(self) -> None:
        plan = self._plan_with_gated_changes()
        self._activate_plan(str(plan.relative_to(self.repo)))
        cfg = self.opsx_plan.planref.load_plan(plan)
        state = self.opsx_plan.state_mod.load_state(self.repo, "test-plan")

        import io as _io
        buf = _io.StringIO()
        with mock.patch("sys.stdout", buf):
            self.opsx_plan.cmd_status_inner(cfg, state, header="test", plan_arg=None)
        output = buf.getvalue()
        self.assertIn("\u2192 opsx-plan approve gated-a", output)
        self.assertIn("\u2192 opsx-plan approve gated-b", output)
        self.assertNotIn("\u2192 opsx-plan approve no-gate", output)

    def test_status_shows_reset_guidance_for_failed_changes(self) -> None:
        plan = self._plan_with_gated_changes()
        self._activate_plan(str(plan.relative_to(self.repo)))
        cfg = self.opsx_plan.planref.load_plan(plan)
        state = self.opsx_plan.state_mod.load_state(self.repo, "test-plan")
        self.opsx_plan.state_mod.set_status(state, "gated-a", self.opsx_plan.base.FAILED, "test failure")
        self.opsx_plan.state_mod.set_status(state, "gated-b", self.opsx_plan.base.FAILED, "another failure")
        self.opsx_plan.state_mod.save_state(self.repo, "test-plan", state)

        import io as _io
        buf = _io.StringIO()
        with mock.patch("sys.stdout", buf):
            self.opsx_plan.cmd_status_inner(cfg, state, header="test", plan_arg=None)
        output = buf.getvalue()
        self.assertIn("\u2192 opsx-plan reset gated-a", output)
        self.assertIn("\u2192 opsx-plan reset gated-b", output)
        self.assertNotIn("\u2192 opsx-plan reset no-gate", output)

    def test_status_uses_long_form_when_explicit_plan_differs(self) -> None:
        plan = self._plan_with_gated_changes()
        # Don't activate; provide explicit plan path
        cfg = self.opsx_plan.planref.load_plan(plan)
        state = self.opsx_plan.state_mod.load_state(self.repo, "test-plan")

        import io as _io
        buf = _io.StringIO()
        plan_arg = "openspec/plans/test.toml"
        with mock.patch("sys.stdout", buf):
            self.opsx_plan.cmd_status_inner(cfg, state, header="test", plan_arg=plan_arg)
        output = buf.getvalue()
        self.assertIn(f"\u2192 opsx-plan approve {plan_arg} gated-a", output)

    def test_single_change_approve_still_works(self) -> None:
        plan = self._plan_with_gated_changes()
        self._activate_plan(str(plan.relative_to(self.repo)))

        args = argparse.Namespace(
            repo=str(self.repo),
            plan=None,
            change=["gated-a"],
            approve_all=False,
        )
        rc = self.opsx_plan.cmd_approve(args)
        self.assertEqual(rc, 0)

        state = self.opsx_plan.state_mod.load_state(self.repo, "test-plan")
        self.assertIn("gated-a", state["approvals"])
        self.assertNotIn("gated-b", state["approvals"])

    def test_single_change_reset_still_works(self) -> None:
        plan = self._plan_with_gated_changes()
        self._activate_plan(str(plan.relative_to(self.repo)))
        state = self.opsx_plan.state_mod.load_state(self.repo, "test-plan")
        self.opsx_plan.state_mod.set_status(state, "gated-a", self.opsx_plan.base.FAILED, "test failure")
        # Ensure gated-b has a record in state (needed after save/load roundtrip)
        self.opsx_plan.state_mod.rec(state, "gated-b")
        self.opsx_plan.state_mod.save_state(self.repo, "test-plan", state)

        args = argparse.Namespace(
            repo=str(self.repo),
            plan=None,
            change=["gated-a"],
            failed=False,
        )
        rc = self.opsx_plan.cmd_reset(args)
        self.assertEqual(rc, 0)

        state = self.opsx_plan.state_mod.load_state(self.repo, "test-plan")
        self.assertEqual(state["changes"]["gated-a"]["status"], self.opsx_plan.base.PENDING)
        self.assertEqual(state["changes"]["gated-b"]["status"], self.opsx_plan.base.PENDING)

    def test_approve_rejects_without_changes_when_not_all(self) -> None:
        plan = self._plan_with_gated_changes()
        self._activate_plan(str(plan.relative_to(self.repo)))

        stderr = io.StringIO()
        args = argparse.Namespace(
            repo=str(self.repo),
            plan=None,
            change=[],
            approve_all=False,
        )
        with mock.patch("sys.stderr", stderr):
            rc = self.opsx_plan.cmd_approve(args)
        self.assertEqual(rc, 2)
        self.assertIn("at least one change id is required", stderr.getvalue())

    def test_status_guidance_includes_accept_for_awaiting_acceptance(self) -> None:
        plan = self._plan_with_created_changes()
        self._activate_plan(str(plan.relative_to(self.repo)))
        cfg = self.opsx_plan.planref.load_plan(plan)
        state = self.opsx_plan.state_mod.load_state(self.repo, "test-plan")
        for cid in ("created-a", "created-b"):
            r = self.opsx_plan.state_mod.rec(state, cid)
            r["created_by_orchestrator"] = True
        self.opsx_plan.state_mod.save_state(self.repo, "test-plan", state)

        import io as _io
        buf = _io.StringIO()
        with mock.patch("sys.stdout", buf):
            self.opsx_plan.cmd_status_inner(cfg, state, header="test", plan_arg=None)
        output = buf.getvalue()
        self.assertIn("\u2192 opsx-plan accept created-a", output)
        self.assertIn("\u2192 opsx-plan accept created-b", output)


class GitDeliveryConfigParsingTests(unittest.TestCase):
    def setUp(self) -> None:
        self.opsx_plan = load_opsx_plan()

    def test_default_disabled(self) -> None:
        cfg = self.opsx_plan.planref._parse_git_delivery_config({})
        self.assertFalse(cfg["enabled"])
        self.assertEqual(cfg["branch"], "")
        self.assertEqual(cfg["base_ref"], "")
        self.assertFalse(cfg["create_pull_request"])

    def test_enabled_with_explicit_branch_and_base(self) -> None:
        cfg = self.opsx_plan.planref._parse_git_delivery_config({
            "enabled": True,
            "branch": "opsx/custom",
            "base_ref": "release/next",
        })
        self.assertTrue(cfg["enabled"])
        self.assertEqual(cfg["branch"], "opsx/custom")
        self.assertEqual(cfg["base_ref"], "release/next")

    def test_create_pull_request_requires_enabled(self) -> None:
        with self.assertRaises(self.opsx_plan.base.PlanError) as ctx:
            self.opsx_plan.planref._parse_git_delivery_config({
                "enabled": False,
                "create_pull_request": True,
            })
        self.assertIn("requires", str(ctx.exception))

    def test_non_bool_enabled_treated_as_false(self) -> None:
        cfg = self.opsx_plan.planref._parse_git_delivery_config({"enabled": "yes"})
        self.assertFalse(cfg["enabled"])

    def test_empty_string_branch_and_base_normalized(self) -> None:
        cfg = self.opsx_plan.planref._parse_git_delivery_config({
            "enabled": True,
            "branch": "  ",
            "base_ref": "  ",
        })
        self.assertEqual(cfg["branch"], "")
        self.assertEqual(cfg["base_ref"], "")


class RunEventNotificationTests(unittest.TestCase):
    """Tests for run-event notification emission points, payload shape, and
    failure isolation (change add-run-event-notifications)."""

    def setUp(self) -> None:
        self.opsx_plan = load_opsx_plan()
        self.tmp = tempfile.TemporaryDirectory()
        self.repo = Path(self.tmp.name)
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
        self.cid = "add-notify-test"
        self.plan_name = "direct-plan"
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
            "notify_cmd": "/usr/bin/env echo",
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
        self.state = {"plan": self.plan_name, "approvals": [], "notified_events": {}, "changes": {}}
        self.write_authored_change(self.cid)
        record = self.opsx_plan.state_mod.rec(self.state, self.cid)
        record["max_rounds"] = self.cfg["max_rounds"]
        record["tracked_change_files"] = self.opsx_plan.state_mod.change_context_paths(
            self.repo, self.cid
        )
        self._saved_invoke = self.opsx_plan.invoke_direct_stage
        self._saved_checks = self.opsx_plan.groundtruth.run_fast_checks
        self._notification_calls: list[tuple[str, str | None, str]] = []

    def tearDown(self) -> None:
        self.opsx_plan.invoke_direct_stage = self._saved_invoke
        self.opsx_plan.groundtruth.run_fast_checks = self._saved_checks
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

    def _patch_notify(self) -> None:
        """Patch _try_notify to capture calls while the real notify_cmd runs."""

        def recording_notify(cfg, event_type, summary, change_id=None):
            self._notification_calls.append((event_type, change_id, summary))

        self.opsx_plan._try_notify = recording_notify

    # ---- 4.1: emission point tests -------------------------------------------

    def test_notify_emitted_on_change_done(self) -> None:
        self._patch_notify()
        self.cfg["notify_cmd"] = "/usr/bin/env echo"

        def fake_invoke(repo, cfg, cid, stage, round_num, input_block):
            log_path = self.opsx_plan.next_stage_log_path(repo, cid, stage, round_num)
            log_path.parent.mkdir(parents=True, exist_ok=True)
            if stage == "implement":
                payload = {
                    "status": "implemented",
                    "change": cid,
                    "round": 1,
                    "progress_made": True,
                    "completed_tasks": ["1.1"],
                    "remaining_tasks": ["1.2"],
                    "task_counts": {"complete": 1, "total": 2},
                    "files_touched": [],
                    "known_change_files": [],
                    "summary": "implemented",
                }
            elif stage == "review":
                payload = {
                    "status": "reviewed",
                    "change": cid,
                    "round": 1,
                    "verdict": "pass",
                    "finding_counts": {"critical": 0, "warning": 0, "note": 0},
                    "summary": "review passed",
                    "fix_prompt": "",
                    "next_phase": "archive",
                }
            else:
                archive_path, commit = self.archive_change_in_repo(cid)
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

        self.assertEqual(result, self.opsx_plan.base.DONE)
        done_events = [c for c in self._notification_calls if c[0] == "change_done"]
        self.assertEqual(len(done_events), 1, f"expected 1 change_done, got {self._notification_calls}")
        self.assertEqual(done_events[0][1], self.cid)

    def test_notify_emitted_on_change_failed_implement_blocked(self) -> None:
        self._patch_notify()

        def fake_invoke(repo, cfg, cid, stage, round_num, input_block):
            log_path = self.opsx_plan.next_stage_log_path(repo, cid, stage, round_num)
            log_path.parent.mkdir(parents=True, exist_ok=True)
            payload = {
                "status": "blocked",
                "change": cid,
                "round": 1,
                "reason": "cannot proceed without spec",
                "summary": "implement blocked",
            }
            log_path.write_text(json.dumps(payload) + "\n", encoding="utf-8")
            return "exited", log_path

        self.opsx_plan.invoke_direct_stage = fake_invoke

        result = self.opsx_plan.run_direct_change(self.repo, self.cfg, self.state, self.cid)

        self.assertEqual(result, "stop")
        failed_events = [c for c in self._notification_calls if c[0] == "change_failed"]
        self.assertEqual(len(failed_events), 1, f"expected 1 change_failed, got {self._notification_calls}")
        self.assertEqual(failed_events[0][1], self.cid)

    def test_notify_emitted_on_change_failed_spawn_error(self) -> None:
        self._patch_notify()

        def fake_invoke(repo, cfg, cid, stage, round_num, input_block):
            log_path = self.opsx_plan.next_stage_log_path(repo, cid, stage, round_num)
            return "spawn_error", log_path

        self.opsx_plan.invoke_direct_stage = fake_invoke

        result = self.opsx_plan.run_direct_change(self.repo, self.cfg, self.state, self.cid)

        self.assertEqual(result, "spawn_error")
        failed_events = [c for c in self._notification_calls if c[0] == "change_failed"]
        self.assertEqual(len(failed_events), 1)

    def test_notify_emitted_on_change_failed_timeout(self) -> None:
        self._patch_notify()

        def fake_invoke(repo, cfg, cid, stage, round_num, input_block):
            log_path = self.opsx_plan.next_stage_log_path(repo, cid, stage, round_num)
            return "timeout", log_path

        self.opsx_plan.invoke_direct_stage = fake_invoke

        result = self.opsx_plan.run_direct_change(self.repo, self.cfg, self.state, self.cid)

        self.assertEqual(result, "failed")
        failed_events = [c for c in self._notification_calls if c[0] == "change_failed"]
        self.assertEqual(len(failed_events), 1)

    def test_notify_emitted_on_change_failed_parse_error(self) -> None:
        self._patch_notify()

        def fake_invoke(repo, cfg, cid, stage, round_num, input_block):
            log_path = self.opsx_plan.next_stage_log_path(repo, cid, stage, round_num)
            log_path.parent.mkdir(parents=True, exist_ok=True)
            log_path.write_text("not json at all\n", encoding="utf-8")
            return "exited", log_path

        self.opsx_plan.invoke_direct_stage = fake_invoke

        result = self.opsx_plan.run_direct_change(self.repo, self.cfg, self.state, self.cid)

        self.assertEqual(result, "failed")
        failed_events = [c for c in self._notification_calls if c[0] == "change_failed"]
        self.assertEqual(len(failed_events), 1)

    def test_notify_emitted_on_review_max_rounds(self) -> None:
        self._patch_notify()
        self.cfg["max_rounds"] = 1
        record = self.opsx_plan.state_mod.rec(self.state, self.cid)
        record["max_rounds"] = 1

        def fake_invoke(repo, cfg, cid, stage, round_num, input_block):
            log_path = self.opsx_plan.next_stage_log_path(repo, cid, stage, round_num)
            log_path.parent.mkdir(parents=True, exist_ok=True)
            if stage == "implement":
                payload = {
                    "status": "implemented",
                    "change": cid,
                    "round": 1,
                    "progress_made": True,
                    "completed_tasks": [],
                    "remaining_tasks": ["1.1", "1.2"],
                    "task_counts": {"complete": 0, "total": 2},
                    "files_touched": [],
                    "known_change_files": [],
                    "summary": "implemented",
                }
            else:
                payload = {
                    "status": "reviewed",
                    "change": cid,
                    "round": 1,
                    "verdict": "fail",
                    "finding_counts": {"critical": 1, "warning": 0, "note": 0},
                    "summary": "review failed",
                    "fix_prompt": "Needs more work.",
                    "next_phase": "implement",
                }
            log_path.write_text(json.dumps(payload) + "\n", encoding="utf-8")
            return "exited", log_path

        self.opsx_plan.invoke_direct_stage = fake_invoke

        result = self.opsx_plan.run_direct_change(self.repo, self.cfg, self.state, self.cid)

        self.assertEqual(result, "stop")
        failed_events = [c for c in self._notification_calls if c[0] == "change_failed"]
        self.assertEqual(len(failed_events), 1)
        self.assertIn("retry budget", failed_events[0][2])

    def test_notify_emitted_on_archive_verification_failure(self) -> None:
        self._patch_notify()

        def fake_invoke(repo, cfg, cid, stage, round_num, input_block):
            log_path = self.opsx_plan.next_stage_log_path(repo, cid, stage, round_num)
            log_path.parent.mkdir(parents=True, exist_ok=True)
            if stage == "implement":
                payload = {
                    "status": "implemented",
                    "change": cid,
                    "round": 1,
                    "progress_made": True,
                    "completed_tasks": ["1.1"],
                    "remaining_tasks": [],
                    "task_counts": {"complete": 1, "total": 2},
                    "files_touched": [],
                    "known_change_files": [],
                    "summary": "implemented",
                }
            elif stage == "review":
                payload = {
                    "status": "reviewed",
                    "change": cid,
                    "round": 1,
                    "verdict": "pass",
                    "finding_counts": {"critical": 0, "warning": 0, "note": 0},
                    "summary": "review passed",
                    "fix_prompt": "",
                    "next_phase": "archive",
                }
            else:
                # Archive claims success but no actual repo evidence
                payload = {
                    "status": "archived",
                    "change": cid,
                    "archive_path": f"openspec/changes/archive/2026-07-02-{cid}",
                    "spec_sync_status": "no-delta",
                    "commit": "deadbeef",
                    "summary": "archive claimed success",
                }
            log_path.write_text(json.dumps(payload) + "\n", encoding="utf-8")
            return "exited", log_path

        self.opsx_plan.invoke_direct_stage = fake_invoke

        result = self.opsx_plan.run_direct_change(self.repo, self.cfg, self.state, self.cid)

        self.assertEqual(result, "stop")
        failed_events = [c for c in self._notification_calls if c[0] == "change_failed"]
        self.assertEqual(len(failed_events), 1)

    # ---- 4.2: payload shape tests -------------------------------------------

    def test_change_specific_payload_includes_change_id(self) -> None:
        payload_json = self.opsx_plan._build_notification_payload(
            event_type="change_done",
            plan_name="test-plan",
            summary="change completed",
            change_id="my-change",
        )
        payload = json.loads(payload_json)
        self.assertEqual(payload["event_type"], "change_done")
        self.assertEqual(payload["plan_name"], "test-plan")
        self.assertIn("timestamp", payload)
        self.assertEqual(payload["summary"], "change completed")
        self.assertEqual(payload["change_id"], "my-change")

    def test_plan_wide_payload_omits_change_id(self) -> None:
        payload_json = self.opsx_plan._build_notification_payload(
            event_type="plan_complete",
            plan_name="test-plan",
            summary="plan finished",
        )
        payload = json.loads(payload_json)
        self.assertEqual(payload["event_type"], "plan_complete")
        self.assertEqual(payload["plan_name"], "test-plan")
        self.assertIn("timestamp", payload)
        self.assertEqual(payload["summary"], "plan finished")
        self.assertNotIn("change_id", payload)

    def test_plan_wide_payload_with_none_change_id_omits_it(self) -> None:
        payload_json = self.opsx_plan._build_notification_payload(
            event_type="plan_complete",
            plan_name="test-plan",
            summary="plan finished",
            change_id=None,
        )
        payload = json.loads(payload_json)
        self.assertNotIn("change_id", payload)

    # ---- 4.3: failure isolation tests ----------------------------------------

    def test_notify_failure_does_not_change_run_outcome(self) -> None:
        self.cfg["notify_cmd"] = "/nonexistent/command/that/will/fail"

        def fake_invoke(repo, cfg, cid, stage, round_num, input_block):
            log_path = self.opsx_plan.next_stage_log_path(repo, cid, stage, round_num)
            log_path.parent.mkdir(parents=True, exist_ok=True)
            if stage == "implement":
                payload = {
                    "status": "implemented",
                    "change": cid,
                    "round": 1,
                    "progress_made": True,
                    "completed_tasks": ["1.1"],
                    "remaining_tasks": ["1.2"],
                    "task_counts": {"complete": 1, "total": 2},
                    "files_touched": [],
                    "known_change_files": [],
                    "summary": "implemented",
                }
            elif stage == "review":
                payload = {
                    "status": "reviewed",
                    "change": cid,
                    "round": 1,
                    "verdict": "pass",
                    "finding_counts": {"critical": 0, "warning": 0, "note": 0},
                    "summary": "review passed",
                    "fix_prompt": "",
                    "next_phase": "archive",
                }
            else:
                archive_path, commit = self.archive_change_in_repo(cid)
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

        # Should still complete successfully despite notification hook failure
        result = self.opsx_plan.run_direct_change(self.repo, self.cfg, self.state, self.cid)

        self.assertEqual(result, self.opsx_plan.base.DONE)
        record = self.opsx_plan.state_mod.rec(self.state, self.cid)
        self.assertEqual(record["status"], self.opsx_plan.base.DONE)

    def test_notify_cmd_not_set_preserves_behavior(self) -> None:
        self.cfg["notify_cmd"] = ""

        def fake_invoke(repo, cfg, cid, stage, round_num, input_block):
            log_path = self.opsx_plan.next_stage_log_path(repo, cid, stage, round_num)
            log_path.parent.mkdir(parents=True, exist_ok=True)
            if stage == "implement":
                payload = {
                    "status": "implemented",
                    "change": cid,
                    "round": 1,
                    "progress_made": True,
                    "completed_tasks": ["1.1"],
                    "remaining_tasks": ["1.2"],
                    "task_counts": {"complete": 1, "total": 2},
                    "files_touched": [],
                    "known_change_files": [],
                    "summary": "implemented",
                }
            elif stage == "review":
                payload = {
                    "status": "reviewed",
                    "change": cid,
                    "round": 1,
                    "verdict": "pass",
                    "finding_counts": {"critical": 0, "warning": 0, "note": 0},
                    "summary": "review passed",
                    "fix_prompt": "",
                    "next_phase": "archive",
                }
            else:
                archive_path, commit = self.archive_change_in_repo(cid)
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

        self.assertEqual(result, self.opsx_plan.base.DONE)
        record = self.opsx_plan.state_mod.rec(self.state, self.cid)
        self.assertEqual(record["status"], self.opsx_plan.base.DONE)

    def test_build_notification_payload_has_required_fields(self) -> None:
        payload_json = self.opsx_plan._build_notification_payload(
            event_type="awaiting_approval",
            plan_name="my-plan",
            summary="awaiting approval for change",
            change_id="ch-1",
        )
        payload = json.loads(payload_json)
        required = {"event_type", "plan_name", "timestamp", "summary"}
        self.assertTrue(required.issubset(set(payload.keys())),
                        f"missing required fields: {required - set(payload.keys())}")
        self.assertEqual(payload["change_id"], "ch-1")

    def test_build_notification_payload_valid_json(self) -> None:
        payload_json = self.opsx_plan._build_notification_payload(
            event_type="change_failed",
            plan_name="plan-with-unicode-\u2603",
            summary="failed with special chars: \n\t\"",
            change_id="ch-fail",
        )
        # Must parse without error (ensure_ascii=False keeps unicode)
        payload = json.loads(payload_json)
        self.assertEqual(payload["plan_name"], "plan-with-unicode-\u2603")

    # ---- 4.4: awaiting_acceptance + plan_complete / pull_request_opened coverage ----

    def test_awaiting_acceptance_notification_emitted(self) -> None:
        """Verify that awaiting_acceptance notification is emitted via cmd_run
        when a created change has not yet been accepted."""
        self.cid = "accept-change"
        self.write_authored_change(self.cid)
        self.plan_name = "accept-plan"

        plan_rel = self._write_plan_toml(
            self.plan_name,
            extra_plan='notify_cmd = "/usr/bin/env echo"\nreview_created = true',
            changes=[{"id": self.cid}],
        )

        # Pre-populate state: change created by orchestrator but not accepted
        state_path = self.opsx_plan.state_mod.state_path(self.repo, self.plan_name)
        state_path.parent.mkdir(parents=True, exist_ok=True)
        initial_state = {
            "plan": self.plan_name,
            "approvals": [],
            "notified_events": {},
            "changes": {
                self.cid: {
                    "status": self.opsx_plan.base.PENDING,
                    "phase": "implement",
                    "round": 1,
                    "max_rounds": 2,
                    "no_progress_streak": 0,
                    "latest_fix_prompt": "",
                    "last_result": "",
                    "task_counts": {"complete": 0, "total": 2},
                    "tracked_change_files": [],
                    "context_cache": self.opsx_plan.state_mod.default_context_cache(),
                    "last_review": self.opsx_plan.state_mod.default_last_review(),
                    "archive": self.opsx_plan.state_mod.default_archive_state(),
                    "history": [],
                    "telemetry": {"latest_telemetry": ""},
                    "change": self.cid,
                    "attempts": 0,
                    "reason": "",
                    "updated_at": "",
                    "create_attempts": 0,
                    "created_by_orchestrator": True,
                    "accepted": False,
                    "last_stage": self.opsx_plan.state_mod.default_last_stage(),
                    "last_log": "",
                }
            },
        }
        with open(state_path, "w", encoding="utf-8") as f:
            json.dump(initial_state, f)

        calls: list[tuple[str, str | None, str]] = []

        def capture_notify(cfg, event_type, summary, change_id=None):
            calls.append((event_type, change_id, summary))

        with mock.patch.object(self.opsx_plan, "_try_notify", side_effect=capture_notify):
            args = argparse.Namespace(
                repo=str(self.repo), plan=str(plan_rel),
                dry_run=False, max_changes=None, budget_minutes=None,
                budget_usd=None, create_only=False, only=None,
                no_branch=False, no_pr=False,
            )
            rc = self.opsx_plan.cmd_run(args)

        # Verify notification was emitted
        accepting = [c for c in calls if c[0] == "awaiting_acceptance"]
        self.assertEqual(len(accepting), 1, f"expected 1 awaiting_acceptance, got {calls}")
        self.assertEqual(accepting[0][1], self.cid)

        # Verify state persisted with notified_events
        state = self.opsx_plan.state_mod.load_state(self.repo, self.plan_name)
        ne = state.get("notified_events", {})
        self.assertIn(self.cid, ne, f"notified_events missing {self.cid}: {ne}")
        self.assertIn("awaiting_acceptance", ne[self.cid])

    def test_plan_complete_emitted_when_all_done(self) -> None:
        """Verify plan_complete notification is emitted when all changes reach
        done with valid archive evidence so cmd_run reaches the plan-wide branch."""
        self.cid = "pc-change"
        self.write_authored_change(self.cid)
        self.plan_name = "pc-emit-plan"

        # Archive the change for real repo evidence
        archive_path, commit = self.archive_change_in_repo(self.cid)

        plan_rel = self._write_plan_toml(
            self.plan_name,
            extra_plan='notify_cmd = "/usr/bin/env echo"',
            changes=[{"id": self.cid}],
        )

        # Pre-populate state: change is done with valid archive evidence
        state_path = self.opsx_plan.state_mod.state_path(self.repo, self.plan_name)
        state_path.parent.mkdir(parents=True, exist_ok=True)
        initial_state = {
            "plan": self.plan_name,
            "approvals": [],
            "notified_events": {},
            "changes": {
                self.cid: {
                    "status": self.opsx_plan.base.DONE,
                    "phase": "done",
                    "round": 1,
                    "max_rounds": 2,
                    "no_progress_streak": 0,
                    "latest_fix_prompt": "",
                    "last_result": "archive_passed",
                    "task_counts": {"complete": 2, "total": 2},
                    "tracked_change_files": [],
                    "context_cache": self.opsx_plan.state_mod.default_context_cache(),
                    "last_review": self.opsx_plan.state_mod.default_last_review(),
                    "archive": {
                        "status": "passed",
                        "path": archive_path,
                        "commit": commit,
                        "reason": "",
                        "spec_sync_status": "no-delta",
                        "triage": self.opsx_plan.state_mod.default_archive_state()["triage"],
                    },
                    "history": [],
                    "telemetry": {"latest_telemetry": ""},
                    "change": self.cid,
                    "attempts": 1,
                    "reason": "",
                    "updated_at": "",
                    "create_attempts": 0,
                    "created_by_orchestrator": False,
                    "accepted": False,
                    "last_stage": self.opsx_plan.state_mod.default_last_stage(),
                    "last_log": "",
                }
            },
        }
        with open(state_path, "w", encoding="utf-8") as f:
            json.dump(initial_state, f)

        calls: list[tuple[str, str | None, str]] = []

        def capture_notify(cfg, event_type, summary, change_id=None):
            calls.append((event_type, change_id, summary))

        with mock.patch.object(self.opsx_plan, "_try_notify", side_effect=capture_notify):
            args = argparse.Namespace(
                repo=str(self.repo), plan=str(plan_rel),
                dry_run=False, max_changes=None, budget_minutes=None,
                budget_usd=None, create_only=False, only=None,
                no_branch=False, no_pr=False,
            )
            rc = self.opsx_plan.cmd_run(args)

        # plan_complete must have been emitted
        plan_events = [c for c in calls if c[0] == "plan_complete"]
        self.assertEqual(len(plan_events), 1,
                         f"expected 1 plan_complete, got {calls}")

        # Verify state persisted with notified_events._plan_
        state = self.opsx_plan.state_mod.load_state(self.repo, self.plan_name)
        ne = state.get("notified_events", {})
        self.assertIn("_plan_", ne, f"notified_events missing _plan_: {ne}")
        self.assertIn("plan_complete", ne["_plan_"])

    def test_pull_request_opened_emitted(self) -> None:
        """Verify pull_request_opened notification is emitted when PR delivery
        records pr_opened status. Uses a mock for attempt_pr_delivery to avoid
        real GitHub CLI / git remote dependencies."""
        self.cid = "pr-emit-change"
        self.write_authored_change(self.cid)
        self.plan_name = "pr-emit-plan"

        # Archive the change for real repo evidence
        archive_path, commit = self.archive_change_in_repo(self.cid)

        plan_rel = self._write_plan_toml(
            self.plan_name,
            extra_plan='notify_cmd = "/usr/bin/env echo"',
            changes=[{"id": self.cid}],
        )

        # Pre-populate state: change is done with valid archive evidence
        state_path = self.opsx_plan.state_mod.state_path(self.repo, self.plan_name)
        state_path.parent.mkdir(parents=True, exist_ok=True)
        initial_state = {
            "plan": self.plan_name,
            "approvals": [],
            "notified_events": {},
            "changes": {
                self.cid: {
                    "status": self.opsx_plan.base.DONE,
                    "phase": "done",
                    "round": 1,
                    "max_rounds": 2,
                    "no_progress_streak": 0,
                    "latest_fix_prompt": "",
                    "last_result": "archive_passed",
                    "task_counts": {"complete": 2, "total": 2},
                    "tracked_change_files": [],
                    "context_cache": self.opsx_plan.state_mod.default_context_cache(),
                    "last_review": self.opsx_plan.state_mod.default_last_review(),
                    "archive": {
                        "status": "passed",
                        "path": archive_path,
                        "commit": commit,
                        "reason": "",
                        "spec_sync_status": "no-delta",
                        "triage": self.opsx_plan.state_mod.default_archive_state()["triage"],
                    },
                    "history": [],
                    "telemetry": {"latest_telemetry": ""},
                    "change": self.cid,
                    "attempts": 1,
                    "reason": "",
                    "updated_at": "",
                    "create_attempts": 0,
                    "created_by_orchestrator": False,
                    "accepted": False,
                    "last_stage": self.opsx_plan.state_mod.default_last_stage(),
                    "last_log": "",
                }
            },
        }
        with open(state_path, "w", encoding="utf-8") as f:
            json.dump(initial_state, f)

        calls: list[tuple[str, str | None, str]] = []

        def capture_notify(cfg, event_type, summary, change_id=None):
            calls.append((event_type, change_id, summary))

        # Mock attempt_pr_delivery to simulate successful PR delivery
        pr_url = "https://github.com/example/pr/99"

        def fake_attempt_pr_delivery(repo, cfg, state, no_pr=False):
            gd = state.setdefault("git_delivery", {
                "base_ref": None,
                "branch_name": None,
                "delivery_status": "disabled",
                "pull_request_url": None,
                "remote_name": None,
            })
            gd["delivery_status"] = "pr_opened"
            gd["pull_request_url"] = pr_url
            return True, None

        with mock.patch.object(self.opsx_plan, "_try_notify", side_effect=capture_notify), \
             mock.patch.object(self.opsx_plan.delivery, "attempt_pr_delivery", side_effect=fake_attempt_pr_delivery):
            args = argparse.Namespace(
                repo=str(self.repo), plan=str(plan_rel),
                dry_run=False, max_changes=None, budget_minutes=None,
                budget_usd=None, create_only=False, only=None,
                no_branch=False, no_pr=False,
            )
            rc = self.opsx_plan.cmd_run(args)

        # pull_request_opened must have been emitted
        pr_events = [c for c in calls if c[0] == "pull_request_opened"]
        self.assertEqual(len(pr_events), 1,
                         f"expected 1 pull_request_opened, got {calls}")
        self.assertIn(pr_url, pr_events[0][2])

        # Verify state persisted with notified_events._plan_
        state = self.opsx_plan.state_mod.load_state(self.repo, self.plan_name)
        ne = state.get("notified_events", {})
        self.assertIn("_plan_", ne, f"notified_events missing _plan_: {ne}")
        self.assertIn("pull_request_opened", ne["_plan_"])

    # ---- 4.5: idempotency / notified_events persistence ----------------------

    def _write_plan_toml(self, plan_name: str, extra_plan: str = "",
                         changes: list[dict] | None = None) -> Path:
        """Write a minimal plan TOML and return its repo-relative path."""
        if changes is None:
            changes = []
        lines = [
            '[plan]',
            f'name = "{plan_name}"',
            'adapter = "opencode"',
            'require_clean_tracked = false',
        ]
        if extra_plan:
            lines.append(extra_plan)
        lines.append("")
        for i, c in enumerate(changes):
            lines.append("[[changes]]")
            for key, val in c.items():
                if isinstance(val, bool):
                    lines.append(f'{key} = {"true" if val else "false"}')
                elif isinstance(val, list):
                    lines.append(f'{key} = {json.dumps(val)}')
                else:
                    lines.append(f'{key} = "{val}"')
            if i < len(changes) - 1:
                lines.append("")
        toml = "\n".join(lines) + "\n"

        plan_rel = f"test-notify-{plan_name}.toml"
        plan_path = self.repo / plan_rel
        plan_path.write_text(toml, encoding="utf-8")
        return Path(plan_rel)

    def test_awaiting_approval_notification_persisted_in_state(self) -> None:
        """Verifies that notified_events are persisted when no-ready path emits
        an awaiting notification."""
        self.cid = "awaiting-change"
        self.write_authored_change(self.cid)
        self.plan_name = "awaiting-plan"

        plan_rel = self._write_plan_toml(
            self.plan_name,
            changes=[{"id": self.cid, "pause_before": True}],
        )

        calls: list[tuple[str, str | None, str]] = []

        def capture_notify(cfg, event_type, summary, change_id=None):
            calls.append((event_type, change_id, summary))

        def fake_run_direct(repo, cfg, state, cid, budget_deadline=None, budget_usd=0.0):
            return self.opsx_plan.base.DONE

        with mock.patch.object(self.opsx_plan, "_try_notify", side_effect=capture_notify), \
             mock.patch.object(self.opsx_plan, "run_direct_change", side_effect=fake_run_direct):
            args = argparse.Namespace(
                repo=str(self.repo), plan=str(plan_rel),
                dry_run=False, max_changes=None, budget_minutes=None,
                budget_usd=None, create_only=False, only=None,
                no_branch=False, no_pr=False,
            )
            rc = self.opsx_plan.cmd_run(args)

        # Verify notification was emitted
        awaiting = [c for c in calls if c[0] == "awaiting_approval"]
        self.assertEqual(len(awaiting), 1, f"expected 1 awaiting_approval, got {calls}")
        self.assertEqual(awaiting[0][1], self.cid)

        # Verify state persisted with notified_events
        state = self.opsx_plan.state_mod.load_state(self.repo, self.plan_name)
        ne = state.get("notified_events", {})
        self.assertIn(self.cid, ne, f"notified_events missing {self.cid}: {ne}")
        self.assertIn("awaiting_approval", ne[self.cid])

    def test_plan_complete_not_reemitted_on_rerun(self) -> None:
        """Verifies that plan_complete notification is not re-emitted when it
        already exists in notified_events."""
        self.cid = "done-change"
        self.write_authored_change(self.cid)
        self.plan_name = "completed-plan"

        # Archive the change so reconcile keeps it done via real repository evidence
        archive_path, commit = self.archive_change_in_repo(self.cid)

        plan_rel = self._write_plan_toml(
            self.plan_name,
            changes=[{"id": self.cid}],
        )

        # Pre-populate state: change is done, plan_complete already notified
        state_path = self.opsx_plan.state_mod.state_path(self.repo, self.plan_name)
        state_path.parent.mkdir(parents=True, exist_ok=True)
        initial_state = {
            "plan": self.plan_name,
            "approvals": [],
            "notified_events": {
                self.cid: [],
                "_plan_": ["plan_complete"],
            },
            "changes": {
                self.cid: {
                    "status": self.opsx_plan.base.DONE,
                    "phase": "done",
                    "round": 1,
                    "max_rounds": 2,
                    "no_progress_streak": 0,
                    "latest_fix_prompt": "",
                    "last_result": "archive_passed",
                    "task_counts": {"complete": 2, "total": 2},
                    "tracked_change_files": [],
                    "context_cache": self.opsx_plan.state_mod.default_context_cache(),
                    "last_review": self.opsx_plan.state_mod.default_last_review(),
                    "archive": {
                        "status": "passed",
                        "path": archive_path,
                        "commit": commit,
                        "reason": "",
                        "spec_sync_status": "no-delta",
                        "triage": self.opsx_plan.state_mod.default_archive_state()["triage"],
                    },
                    "history": [],
                    "telemetry": {"latest_telemetry": ""},
                    "change": self.cid,
                    "attempts": 1,
                    "reason": "",
                    "updated_at": "",
                    "create_attempts": 0,
                    "created_by_orchestrator": False,
                    "accepted": False,
                    "last_stage": self.opsx_plan.state_mod.default_last_stage(),
                    "last_log": "",
                }
            },
        }
        with open(state_path, "w", encoding="utf-8") as f:
            json.dump(initial_state, f)

        calls: list[tuple[str, str | None, str]] = []

        def capture_notify(cfg, event_type, summary, change_id=None):
            calls.append((event_type, change_id, summary))

        with mock.patch.object(self.opsx_plan, "_try_notify", side_effect=capture_notify):
            args = argparse.Namespace(
                repo=str(self.repo), plan=str(plan_rel),
                dry_run=False, max_changes=None, budget_minutes=None,
                budget_usd=None, create_only=False, only=None,
                no_branch=False, no_pr=False,
            )
            rc = self.opsx_plan.cmd_run(args)

        # plan_complete must NOT be re-emitted
        plan_events = [c for c in calls if c[0] == "plan_complete"]
        self.assertEqual(len(plan_events), 0,
                         f"plan_complete was re-emitted on rerun: {calls}")

    def test_pull_request_opened_not_reemitted_on_rerun(self) -> None:
        """Verifies that pull_request_opened notification is not re-emitted when
        it already exists in notified_events."""
        self.cid = "pr-change"
        self.write_authored_change(self.cid)
        self.plan_name = "pr-plan"

        # Archive the change so reconcile keeps it done via real repository evidence
        archive_path, commit = self.archive_change_in_repo(self.cid)

        plan_rel = self._write_plan_toml(
            self.plan_name,
            extra_plan='[plan.git_delivery]\nenabled = false\ncreate_pull_request = false',
            changes=[{"id": self.cid}],
        )

        # Pre-populate state: change is done, PR already notified
        state_path = self.opsx_plan.state_mod.state_path(self.repo, self.plan_name)
        state_path.parent.mkdir(parents=True, exist_ok=True)
        initial_state = {
            "plan": self.plan_name,
            "approvals": [],
            "notified_events": {
                self.cid: [],
                "_plan_": ["plan_complete", "pull_request_opened"],
            },
            "git_delivery": {
                "base_ref": "main",
                "branch_name": "opsx/pr-plan",
                "delivery_status": "pr_opened",
                "pull_request_url": "https://github.com/example/pr/42",
                "remote_name": "origin",
            },
            "changes": {
                self.cid: {
                    "status": self.opsx_plan.base.DONE,
                    "phase": "done",
                    "round": 1,
                    "max_rounds": 2,
                    "no_progress_streak": 0,
                    "latest_fix_prompt": "",
                    "last_result": "archive_passed",
                    "task_counts": {"complete": 2, "total": 2},
                    "tracked_change_files": [],
                    "context_cache": self.opsx_plan.state_mod.default_context_cache(),
                    "last_review": self.opsx_plan.state_mod.default_last_review(),
                    "archive": {
                        "status": "passed",
                        "path": archive_path,
                        "commit": commit,
                        "reason": "",
                        "spec_sync_status": "no-delta",
                        "triage": self.opsx_plan.state_mod.default_archive_state()["triage"],
                    },
                    "history": [],
                    "telemetry": {"latest_telemetry": ""},
                    "change": self.cid,
                    "attempts": 1,
                    "reason": "",
                    "updated_at": "",
                    "create_attempts": 0,
                    "created_by_orchestrator": False,
                    "accepted": False,
                    "last_stage": self.opsx_plan.state_mod.default_last_stage(),
                    "last_log": "",
                }
            },
        }
        with open(state_path, "w", encoding="utf-8") as f:
            json.dump(initial_state, f)

        calls: list[tuple[str, str | None, str]] = []

        def capture_notify(cfg, event_type, summary, change_id=None):
            calls.append((event_type, change_id, summary))

        with mock.patch.object(self.opsx_plan, "_try_notify", side_effect=capture_notify):
            args = argparse.Namespace(
                repo=str(self.repo), plan=str(plan_rel),
                dry_run=False, max_changes=None, budget_minutes=None,
                budget_usd=None, create_only=False, only=None,
                no_branch=False, no_pr=False,
            )
            rc = self.opsx_plan.cmd_run(args)

        # pull_request_opened must NOT be re-emitted
        pr_events = [c for c in calls if c[0] == "pull_request_opened"]
        self.assertEqual(len(pr_events), 0,
                         f"pull_request_opened was re-emitted on rerun: {calls}")


class SingleChangeManifestTests(unittest.TestCase):
    """7.1–7.4: Manifest serialization round-trip and dirty-tree guard."""

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
        self.cid = "add-manifest-test"

    def tearDown(self) -> None:
        self.tmp.cleanup()

    def write_authored_change(self, cid: str) -> None:
        cdir = self.repo / "openspec" / "changes" / cid
        cdir.mkdir(parents=True)
        (cdir / "proposal.md").write_text("## Why\n", encoding="utf-8")
        (cdir / "tasks.md").write_text(
            "## 1. Tasks\n\n- [ ] 1.1 Example task\n", encoding="utf-8"
        )

    def test_render_round_trips_through_load_plan(self):
        """7.1"""
        self.write_authored_change(self.cid)
        cfg = self.opsx_plan.build_single_change_config(self.repo, self.cid)
        self.opsx_plan.write_single_change_manifest(self.repo, self.cid, cfg)

        manifest_path = self.opsx_plan.planref.single_change_manifest_path(
            self.repo, self.cid
        )
        self.assertTrue(manifest_path.is_file())

        loaded = self.opsx_plan.planref.load_plan(manifest_path, repo=self.repo)
        self.assertEqual(loaded["name"], cfg["name"])
        self.assertEqual(loaded["adapter"], "opencode")
        self.assertFalse(loaded["review_created"],
                         "review_created must be False in the loaded config")
        self.assertIn(self.cid, loaded["changes"])
        self.assertEqual(
            loaded["changes"][self.cid]["id"], self.cid,
        )

    def test_write_rejects_dirty_tracked_guard(self):
        """7.4"""
        self.write_authored_change(self.cid)
        (self.repo / "tracked.txt").write_text("dirty\n", encoding="utf-8")
        args = argparse.Namespace(repo=str(self.repo), change=self.cid)

        # patch run_direct_change so we don't actually spawn
        with mock.patch.object(self.opsx_plan, "run_direct_change") as run_dc:
            stderr = io.StringIO()
            with mock.patch("sys.stderr", stderr):
                rc = self.opsx_plan.cmd_run_one(args)
            self.assertEqual(rc, 2)
            run_dc.assert_not_called()
            self.assertIn("tracked worktree is dirty", stderr.getvalue())

        # Manifest must NOT have been written
        manifest_path = self.opsx_plan.planref.single_change_manifest_path(
            self.repo, self.cid
        )
        self.assertFalse(
            manifest_path.is_file(),
            "manifest must not be written when dirty-tree guard rejects run",
        )

    def test_manifest_written_on_clean_tree(self):
        """7.3"""
        self.write_authored_change(self.cid)
        args = argparse.Namespace(repo=str(self.repo), change=self.cid)

        def fake_run_dc(repo, cfg, state, cid, budget_usd=0.0):
            self.assertEqual(cid, self.cid)
            r = self.opsx_plan.state_mod.rec(state, cid)
            r["phase"] = "done"
            self.opsx_plan.state_mod.set_status(state, cid, self.opsx_plan.base.DONE, "done")
            return self.opsx_plan.base.DONE

        with mock.patch.object(
            self.opsx_plan, "run_direct_change", side_effect=fake_run_dc
        ):
            rc = self.opsx_plan.cmd_run_one(args)

        self.assertEqual(rc, 0)
        manifest_path = self.opsx_plan.planref.single_change_manifest_path(
            self.repo, self.cid
        )
        self.assertTrue(
            manifest_path.is_file(),
            "manifest must be written when run succeeds",
        )

    def test_divergent_serialization_rejects_write_cleans_up(self):
        """7.2 — divergent serialization fails write, removes manifest + temp."""
        self.write_authored_change(self.cid)
        cfg = self.opsx_plan.build_single_change_config(self.repo, self.cid)

        # Write a first valid manifest (to create a file on disk).
        self.opsx_plan.write_single_change_manifest(self.repo, self.cid, cfg)
        manifest_path = self.opsx_plan.planref.single_change_manifest_path(
            self.repo, self.cid
        )
        self.assertTrue(manifest_path.is_file(), "initial manifest must exist")

        # Now inject divergence: modify cfg so the round-trip mismatch triggers
        # _compare_configs's unlink path for both tmp and the existing manifest.
        divergent_cfg = dict(cfg)
        divergent_cfg["name"] = "deliberately-different"
        tmp_path = manifest_path.with_suffix(manifest_path.suffix + ".tmp")
        tmp_path.write_text("", encoding="utf-8")

        with self.assertRaises(self.opsx_plan.base.PlanError) as ctx:
            self.opsx_plan._compare_configs(
                divergent_cfg, cfg, tmp_path, manifest_path,
            )
        self.assertIn("round-trip divergence", str(ctx.exception))
        self.assertIn("name", str(ctx.exception))
        # Both the temp file and the existing manifest must be removed.
        self.assertFalse(tmp_path.is_file(), "temp file must be removed on divergence")
        self.assertFalse(
            manifest_path.is_file(),
            "stale manifest must be removed on divergence",
        )

    def test_cmd_run_one_preserves_active_pointer(self):
        """cmd_run_one must leave a pre-existing active-plan pointer intact."""
        self.write_authored_change(self.cid)
        # Set up an active plan pointer referencing an unrelated plan.
        plans_dir = self.repo / "openspec" / "plans"
        plans_dir.mkdir(parents=True)
        (plans_dir / "existing.toml").write_text(
            '[plan]\nname = "existing"\nadapter = "opencode"\n\n'
            '[[changes]]\nid = "x"\n',
            encoding="utf-8",
        )
        self.opsx_plan.write_active_plan(self.repo, "openspec/plans/existing.toml")
        before = self.opsx_plan.planref.read_active_plan(self.repo)
        self.assertEqual(before, "openspec/plans/existing.toml")

        args = argparse.Namespace(repo=str(self.repo), change=self.cid)

        def fake_run_dc(repo, cfg, state, cid, budget_usd=0.0):
            r = self.opsx_plan.state_mod.rec(state, cid)
            r["phase"] = "done"
            self.opsx_plan.state_mod.set_status(state, cid, self.opsx_plan.base.DONE, "done")
            return self.opsx_plan.base.DONE

        with mock.patch.object(
            self.opsx_plan, "run_direct_change", side_effect=fake_run_dc
        ):
            rc = self.opsx_plan.cmd_run_one(args)

        self.assertEqual(rc, 0)
        # Active pointer must be preserved — unchanged from before the run.
        after = self.opsx_plan.planref.read_active_plan(self.repo)
        self.assertEqual(after, before,
                          "cmd_run_one must preserve the active-plan pointer")

    def test_round_trip_with_nonzero_escalation_threshold(self):
        """2.5: non-zero escalate_after_review_fails survives round-trip"""
        self.write_authored_change(self.cid)
        cfg = self.opsx_plan.build_single_change_config(self.repo, self.cid)
        cfg["escalate_after_review_fails"] = 2
        # Round-trip through render → load → compare — must not raise.
        self.opsx_plan.write_single_change_manifest(self.repo, self.cid, cfg)

    def test_round_trip_with_nonzero_finding_recurrence_limit(self):
        """4.4: non-zero finding_recurrence_limit survives round-trip"""
        self.write_authored_change(self.cid)
        cfg = self.opsx_plan.build_single_change_config(self.repo, self.cid)
        cfg["finding_recurrence_limit"] = 3
        # Round-trip through render → load → compare — must not raise.
        self.opsx_plan.write_single_change_manifest(self.repo, self.cid, cfg)
        reloaded = self.opsx_plan.planref.load_plan(
            self.opsx_plan.planref.single_change_manifest_path(self.repo, self.cid),
            repo=self.repo,
        )
        self.assertEqual(reloaded["finding_recurrence_limit"], 3)



class ForChangeReportTests(unittest.TestCase):
    """7.5–7.6: report --for-change resolution, exercised through
    cmd_report / cmd_dashboard command paths and state-file fallback."""

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
        self.cid = "add-for-change-test"
        self.plan_name = f"run-{self.cid}"

    def tearDown(self) -> None:
        self.tmp.cleanup()

    def write_authored_change(self, cid: str) -> None:
        cdir = self.repo / "openspec" / "changes" / cid
        cdir.mkdir(parents=True)
        (cdir / "proposal.md").write_text("## Why\n", encoding="utf-8")
        (cdir / "tasks.md").write_text(
            "## 1. Tasks\n\n- [ ] 1.1 Example task\n", encoding="utf-8"
        )

    def _write_telemetry_and_state(self) -> None:
        """Create minimal telemetry and state so report/dashboard can render."""
        tele_dir = self.repo / ".opsx-plan" / "telemetry"
        tele_dir.mkdir(parents=True, exist_ok=True)
        record = {
            "schema_version": 1,
            "uid": "uid-001",
            "plan_name": self.plan_name,
            "run_id": "run-001",
            "change_id": self.cid,
            "stage": "implement",
            "round": 1,
            "status": "completed",
            "started_at": "2026-07-01T10:00:00",
            "ended_at": "2026-07-01T10:02:00",
            "duration_ms": 120000,
            "usage": {
                "usage_available": True,
                "input_tokens": 10000,
                "output_tokens": 2000,
                "cached_input_tokens": None,
                "reasoning_tokens": None,
                "total_tokens": 12000,
                "usage_source": "worker_json",
            },
            "cost": {
                "status": "estimated",
                "estimated_cost": 0.05,
                "pricing_catalog_version": None,
                "price_snapshot": None,
                "unresolved_reason": None,
            },
            "model": {
                "provider": "openai",
                "model_id": "gpt-4o",
                "model_alias": None,
            },
            "result": {
                "stage_status": "completed",
                "verdict": None,
                "critical_count": 0,
                "warning_count": 0,
                "note_count": 0,
            },
        }
        (tele_dir / f"{self.plan_name}.jsonl").write_text(
            json.dumps(record) + "\n", encoding="utf-8",
        )
        state_dir = self.repo / ".opsx-plan"
        state_dir.mkdir(parents=True, exist_ok=True)
        (state_dir / f"{self.plan_name}.state.json").write_text(
            json.dumps({"plan": self.plan_name, "approvals": [], "changes": {
                self.cid: {"status": "done", "round": 1, "phase": "done"},
            }}), encoding="utf-8",
        )

    def test_for_change_resolves_via_manifest(self):
        """7.5 — manifest exists"""
        self.write_authored_change(self.cid)
        cfg = self.opsx_plan.build_single_change_config(self.repo, self.cid)
        self.opsx_plan.write_single_change_manifest(self.repo, self.cid, cfg)

        plan = self.opsx_plan.report._resolve_for_change_plan(
            self.repo, self.cid, None,
        )
        self.assertIsNotNone(plan)
        self.assertIn(f"run-{self.cid}", plan)
        self.assertIn(".toml", plan)

    def test_for_change_errors_unknown_id(self):
        """7.6"""
        with self.assertRaises(self.opsx_plan.base.PlanError) as ctx:
            self.opsx_plan.report._resolve_for_change_plan(
                self.repo, "no-such-change", None,
            )
        self.assertIn("no-such-change", str(ctx.exception))

    def test_for_change_mutually_exclusive_with_plan(self):
        """7.6"""
        with self.assertRaises(self.opsx_plan.base.PlanError) as ctx:
            self.opsx_plan.report._resolve_for_change_plan(
                self.repo, self.cid, "some-plan.toml",
            )
        self.assertIn("mutually exclusive", str(ctx.exception))

    def test_for_change_way_through_state_file(self):
        """7.5 — manifest absent, state file exists"""
        self.write_authored_change(self.cid)
        state_dir = self.repo / ".opsx-plan"
        state_dir.mkdir(parents=True, exist_ok=True)
        state_file = state_dir / f"run-{self.cid}.state.json"
        state_file.write_text('{"plan": "run-' + self.cid + '"}', encoding="utf-8")

        plan = self.opsx_plan.report._resolve_for_change_plan(
            self.repo, self.cid, None,
        )
        self.assertEqual(plan, f"run-{self.cid}")

    def test_cmd_report_for_change_via_manifest(self):
        """Exercise ``cmd_report --for-change`` through the manifest path."""
        self.write_authored_change(self.cid)
        cfg = self.opsx_plan.build_single_change_config(self.repo, self.cid)
        self.opsx_plan.write_single_change_manifest(self.repo, self.cid, cfg)
        self._write_telemetry_and_state()

        stdout = io.StringIO()
        stderr = io.StringIO()
        args = argparse.Namespace(
            repo=str(self.repo), plan=None, json=False,
            change=None, run_id=None, stage=None, model=None,
            for_change=self.cid,
        )
        with mock.patch("sys.stdout", stdout), mock.patch("sys.stderr", stderr):
            rc = self.opsx_plan.report.cmd_report(args)
        self.assertEqual(rc, 0, f"report failed: {stderr.getvalue()}")
        self.assertIn(self.plan_name, stdout.getvalue())

    def test_cmd_report_for_change_via_state_file_fallback(self):
        """Exercise ``cmd_report --for-change`` through the state-file
        fallback when no manifest exists."""
        self.write_authored_change(self.cid)
        self._write_telemetry_and_state()

        stdout = io.StringIO()
        stderr = io.StringIO()
        args = argparse.Namespace(
            repo=str(self.repo), plan=None, json=False,
            change=None, run_id=None, stage=None, model=None,
            for_change=self.cid,
        )
        with mock.patch("sys.stdout", stdout), mock.patch("sys.stderr", stderr):
            rc = self.opsx_plan.report.cmd_report(args)
        self.assertEqual(rc, 0, f"report fallback failed: {stderr.getvalue()}")
        self.assertIn(self.plan_name, stdout.getvalue())

    def test_cmd_dashboard_for_change_via_manifest(self):
        """Exercise ``cmd_dashboard --for-change`` through the manifest path."""
        self.write_authored_change(self.cid)
        cfg = self.opsx_plan.build_single_change_config(self.repo, self.cid)
        self.opsx_plan.write_single_change_manifest(self.repo, self.cid, cfg)
        self._write_telemetry_and_state()

        output = self.repo / "out.html"
        stderr = io.StringIO()
        args = argparse.Namespace(
            repo=str(self.repo), plan=None, output=str(output),
            change=None, run_id=None,
            for_change=self.cid,
        )
        with mock.patch("sys.stderr", stderr):
            rc = self.opsx_plan.dashboard.cmd_dashboard(args)
        self.assertEqual(rc, 0, f"dashboard failed: {stderr.getvalue()}")
        self.assertTrue(output.is_file(), "dashboard HTML must be written")
        content = output.read_text(encoding="utf-8")
        self.assertIn("<html", content)

    def test_cmd_dashboard_for_change_via_state_file_fallback(self):
        """Exercise ``cmd_dashboard --for-change`` through the state-file
        fallback when no manifest exists."""
        self.write_authored_change(self.cid)
        self._write_telemetry_and_state()

        output = self.repo / "out.html"
        stderr = io.StringIO()
        args = argparse.Namespace(
            repo=str(self.repo), plan=None, output=str(output),
            change=None, run_id=None,
            for_change=self.cid,
        )
        with mock.patch("sys.stderr", stderr):
            rc = self.opsx_plan.dashboard.cmd_dashboard(args)
        self.assertEqual(rc, 0, f"dashboard fallback failed: {stderr.getvalue()}")
        self.assertTrue(output.is_file(), "dashboard HTML must be written")
        content = output.read_text(encoding="utf-8")
        self.assertIn("<html", content)


class ArchivePlanCommandTests(unittest.TestCase):
    """7.7: archive-plan moves files and handles pointer."""

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

    def _write_plan_pair(self, name: str, content: str = "") -> tuple[Path, Path]:
        plans_dir = self.repo / "openspec" / "plans"
        plans_dir.mkdir(parents=True, exist_ok=True)
        toml_path = plans_dir / f"{name}.toml"
        md_path = plans_dir / f"{name}.md"
        toml_path.write_text(
            '[plan]\nname = "test"\nadapter = "opencode"\n\n'
            '[[changes]]\nid = "c1"\n',
            encoding="utf-8",
        )
        md_path.write_text(content or "# Test Plan\n", encoding="utf-8")
        return toml_path, md_path

    def test_archive_moves_both_files(self):
        toml_path, md_path = self._write_plan_pair("my-plan")
        git(self.repo, "add", str(toml_path.relative_to(self.repo)))
        git(self.repo, "add", str(md_path.relative_to(self.repo)))

        args = argparse.Namespace(
            repo=str(self.repo),
            plan=str(toml_path.relative_to(self.repo)),
        )
        rc = self.opsx_plan.cmd_archive_plan(args)
        self.assertEqual(rc, 0)

        archived_dir = self.repo / "openspec" / "plans" / "archived"
        self.assertTrue((archived_dir / "my-plan.toml").is_file())
        self.assertTrue((archived_dir / "my-plan.md").is_file())
        self.assertFalse(toml_path.exists())
        self.assertFalse(md_path.exists())

    def test_archive_clears_matching_pointer(self):
        toml_path, md_path = self._write_plan_pair("my-plan")
        rel = str(toml_path.relative_to(self.repo))
        self.opsx_plan.write_active_plan(self.repo, rel)

        args = argparse.Namespace(
            repo=str(self.repo),
            plan=rel,
        )
        rc = self.opsx_plan.cmd_archive_plan(args)
        self.assertEqual(rc, 0)

        self.assertIsNone(self.opsx_plan.planref.read_active_plan(self.repo),
                          "active-plan pointer must be cleared")

    def test_archive_refuses_already_archived(self):
        archived_dir = self.repo / "openspec" / "plans" / "archived"
        archived_dir.mkdir(parents=True)
        toml_path = archived_dir / "my-plan.toml"
        toml_path.write_text(
            '[plan]\nname = "test"\nadapter = "opencode"\n\n'
            '[[changes]]\nid = "c1"\n',
            encoding="utf-8",
        )

        args = argparse.Namespace(
            repo=str(self.repo),
            plan=str(toml_path.relative_to(self.repo)),
        )
        stderr = io.StringIO()
        with mock.patch("sys.stderr", stderr):
            rc = self.opsx_plan.cmd_archive_plan(args)
        self.assertEqual(rc, 2)
        self.assertIn("already under", stderr.getvalue())

    def test_archive_refuses_outside_plans_dir(self):
        toml_path = self.repo / "plan.toml"
        toml_path.write_text(
            '[plan]\nname = "test"\nadapter = "opencode"\n\n'
            '[[changes]]\nid = "c1"\n',
            encoding="utf-8",
        )

        args = argparse.Namespace(
            repo=str(self.repo),
            plan="plan.toml",
        )
        stderr = io.StringIO()
        with mock.patch("sys.stderr", stderr):
            rc = self.opsx_plan.cmd_archive_plan(args)
        self.assertEqual(rc, 2)
        self.assertIn("must be under openspec/plans/", stderr.getvalue())

    def test_archive_refuses_missing_target(self):
        """7.7 — archive-plan refuses a path that does not exist."""
        args = argparse.Namespace(
            repo=str(self.repo),
            plan="openspec/plans/nonexistent.toml",
        )
        stderr = io.StringIO()
        with mock.patch("sys.stderr", stderr):
            rc = self.opsx_plan.cmd_archive_plan(args)
        self.assertEqual(rc, 2)
        self.assertIn("not found", stderr.getvalue())

    def test_archive_without_md_sibling(self):
        """7.7 — archive-plan moves the .toml when no .md sibling exists."""
        plans_dir = self.repo / "openspec" / "plans"
        plans_dir.mkdir(parents=True, exist_ok=True)
        toml_path = plans_dir / "solo-plan.toml"
        toml_path.write_text(
            '[plan]\nname = "solo"\nadapter = "opencode"\n\n'
            '[[changes]]\nid = "c1"\n',
            encoding="utf-8",
        )
        args = argparse.Namespace(
            repo=str(self.repo),
            plan="openspec/plans/solo-plan.toml",
        )
        rc = self.opsx_plan.cmd_archive_plan(args)
        self.assertEqual(rc, 0)
        archived_dir = self.repo / "openspec" / "plans" / "archived"
        self.assertTrue((archived_dir / "solo-plan.toml").is_file())
        self.assertFalse((archived_dir / "solo-plan.md").is_file())
        self.assertFalse(toml_path.exists())

    def test_archive_leaves_nonmatching_pointer_intact(self):
        """7.7 — archiving a non-active plan must preserve the active-plan
        pointer that references a different plan."""
        # Create plan-a (the one we'll archive)
        toml_a, md_a = self._write_plan_pair("plan-a")
        rel_a = str(toml_a.relative_to(self.repo))
        # Create plan-b (the active plan we must preserve)
        toml_b, md_b = self._write_plan_pair("plan-b")
        rel_b = str(toml_b.relative_to(self.repo))
        # Point active to plan-b
        self.opsx_plan.write_active_plan(self.repo, rel_b)

        args = argparse.Namespace(
            repo=str(self.repo),
            plan=rel_a,
        )
        rc = self.opsx_plan.cmd_archive_plan(args)
        self.assertEqual(rc, 0)

        # plan-a should be moved to archived/
        archived_dir = self.repo / "openspec" / "plans" / "archived"
        self.assertTrue((archived_dir / "plan-a.toml").is_file())
        self.assertFalse(toml_a.exists())

        # plan-b's active pointer must still be intact
        self.assertEqual(
            self.opsx_plan.planref.read_active_plan(self.repo), rel_b,
            "active-plan pointer referencing a different plan must be preserved",
        )
class ModelsCommandTests(unittest.TestCase):
    """5.4: cmd_models_show/env/init handle the optional escalation role."""

    def setUp(self) -> None:
        self.opsx_plan = load_opsx_plan()
        self.tmp = tempfile.TemporaryDirectory()
        self.repo = Path(self.tmp.name)
        # Save env vars so tests don't leak side effects.
        self._saved_env = {}
        for key in ("OPSX_IMPLEMENTER_MODEL", "OPSX_IMPLEMENTER_ESCALATION_MODEL",
                     "OPSX_REVIEWER_MODEL", "OPSX_ARCHIVER_MODEL",
                     "OPSX_CONTROLLER_MODEL"):
            self._saved_env[key] = os.environ.get(key)
        # Isolate model resolution from the real machine's home directory.
        from lib.models import resolver as _resolver
        self._models_patch = mock.patch.object(
            _resolver, "USER_CONFIG_PATH",
            Path(self.tmp.name) / "unused-home" / "models.toml"
        )
        self._models_patch.start()
        self.addCleanup(self._models_patch.stop)
        # cmd_models_init uses opsx_plan's local USER_CONFIG_PATH (imported
        # via ``from lib.models.resolver import ...`` at module load time),
        # so patching only resolver.USER_CONFIG_PATH leaves the module-level
        # name pointing at the real user-global file.  Patch the module
        # attribute so the generated models.toml lands in the temp tree.
        self._module_config_patch = mock.patch.object(
            self.opsx_plan, "USER_CONFIG_PATH",
            Path(self.tmp.name) / "unused-home" / "models.toml",
        )
        self._module_config_patch.start()
        self.addCleanup(self._module_config_patch.stop)

    def tearDown(self) -> None:
        # Restore env vars that tests may have modified.
        for key, val in self._saved_env.items():
            if val is not None:
                os.environ[key] = val
            else:
                os.environ.pop(key, None)
        self.tmp.cleanup()

    def _write_config(self, content: str) -> None:
        cfg_path = self.repo / ".opsx-plan" / "models.toml"
        cfg_path.parent.mkdir(parents=True, exist_ok=True)
        cfg_path.write_text(textwrap.dedent(content), encoding="utf-8")

    def test_models_show_includes_escalation(self) -> None:
        """5.4: models show prints escalation row"""
        self._write_config(
            """\
            [adapters.opencode]
            controller = "github-copilot/gpt-5.4"
            implementer = "deepseek/deepseek-v4-pro"
            implementer_escalation = "deepseek/deepseek-v4-ultra"
            reviewer = "github-copilot/gpt-5.4"
            archiver = "github-copilot/gpt-5.4"
            """
        )
        args = argparse.Namespace(repo=str(self.repo), adapter="opencode")
        stdout = io.StringIO()
        with mock.patch("sys.stdout", stdout):
            rc = self.opsx_plan.cmd_models_show(args)
        self.assertEqual(rc, 0)
        output = stdout.getvalue()
        self.assertIn("implementer_escalation", output)
        self.assertIn("deepseek/deepseek-v4-ultra", output)

    def test_models_env_exits_zero_with_unresolved_escalation(self) -> None:
        """5.4: models env exits 0 when escalation is unresolved"""
        os.environ.pop("OPSX_IMPLEMENTER_ESCALATION_MODEL", None)
        self._write_config(
            """\
            [adapters.opencode]
            controller = "github-copilot/gpt-5.4"
            implementer = "deepseek/deepseek-v4-pro"
            reviewer = "github-copilot/gpt-5.4"
            archiver = "github-copilot/gpt-5.4"
            """
        )
        args = argparse.Namespace(repo=str(self.repo), adapter="opencode")
        stdout = io.StringIO()
        with mock.patch("sys.stdout", stdout):
            rc = self.opsx_plan.cmd_models_env(args)
        self.assertEqual(rc, 0,
                         "models env must exit 0 when escalation is unresolved")
        output = stdout.getvalue()
        self.assertIn("OPSX_IMPLEMENTER_MODEL", output)
        self.assertNotIn("OPSX_IMPLEMENTER_ESCALATION_MODEL", output)

    def test_models_env_exports_escalation_when_resolved(self) -> None:
        """5.4: models env emits escalation export when resolved"""
        self._write_config(
            """\
            [adapters.opencode]
            controller = "github-copilot/gpt-5.4"
            implementer = "deepseek/deepseek-v4-pro"
            implementer_escalation = "deepseek/deepseek-v4-ultra"
            reviewer = "github-copilot/gpt-5.4"
            archiver = "github-copilot/gpt-5.4"
            """
        )
        args = argparse.Namespace(repo=str(self.repo), adapter="opencode")
        stdout = io.StringIO()
        with mock.patch("sys.stdout", stdout):
            rc = self.opsx_plan.cmd_models_env(args)
        self.assertEqual(rc, 0)
        output = stdout.getvalue()
        self.assertIn("OPSX_IMPLEMENTER_ESCALATION_MODEL", output)

    def test_models_init_seeds_escalation_from_environment(self) -> None:
        """5.4: models init seeds escalation role from env"""
        saved_controller = os.environ.get("OPSX_CONTROLLER_MODEL", "")
        saved_impl = os.environ.get("OPSX_IMPLEMENTER_MODEL", "")
        saved_esc = os.environ.get("OPSX_IMPLEMENTER_ESCALATION_MODEL", "")
        saved_rev = os.environ.get("OPSX_REVIEWER_MODEL", "")
        saved_arc = os.environ.get("OPSX_ARCHIVER_MODEL", "")
        try:
            os.environ["OPSX_CONTROLLER_MODEL"] = "github-copilot/gpt-5.4"
            os.environ["OPSX_IMPLEMENTER_MODEL"] = "deepseek/deepseek-v4-pro"
            os.environ["OPSX_IMPLEMENTER_ESCALATION_MODEL"] = "deepseek/deepseek-v4-ultra"
            os.environ["OPSX_REVIEWER_MODEL"] = "github-copilot/gpt-5.4"
            os.environ["OPSX_ARCHIVER_MODEL"] = "github-copilot/gpt-5.4"
            args = argparse.Namespace(force=True)
            stdout = io.StringIO()
            with mock.patch("sys.stdout", stdout):
                rc = self.opsx_plan.cmd_models_init(args)
            self.assertEqual(rc, 0)
            output = stdout.getvalue()
            self.assertIn("Created", output)
            # 4 required roles + 1 optional = 5 roles seeded when env has all 5.
            self.assertIn("5 role(s)", output,
                          "must seed all 5 roles including implementer_escalation")
            # Verify ALL_ROLES includes the escalation role (the source of truth).
            self.assertIn(
                "implementer_escalation", self.opsx_plan.ALL_ROLES,
                "ALL_ROLES must include implementer_escalation for init seeding",
            )
            # Verify the generated TOML file contains the optional role.
            models_path = Path(self.tmp.name) / "unused-home" / "models.toml"
            self.assertTrue(models_path.exists(),
                            f"models init must create {models_path}")
            toml_content = models_path.read_text(encoding="utf-8")
            self.assertIn("implementer_escalation", toml_content,
                          "generated TOML must contain implementer_escalation")
            self.assertIn("deepseek/deepseek-v4-ultra", toml_content,
                          "generated TOML must contain the escalation model id")
        finally:
            for key, val in [
                ("OPSX_CONTROLLER_MODEL", saved_controller),
                ("OPSX_IMPLEMENTER_MODEL", saved_impl),
                ("OPSX_IMPLEMENTER_ESCALATION_MODEL", saved_esc),
                ("OPSX_REVIEWER_MODEL", saved_rev),
                ("OPSX_ARCHIVER_MODEL", saved_arc),
            ]:
                if val:
                    os.environ[key] = val
                else:
                    os.environ.pop(key, None)


class ReviewGateSkipSeverityTests(unittest.TestCase):
    """The skip_warning / skip_suggestion gate in apply_review_result.

    Review workers apply a strict rule and recommend `fail` for any non-zero
    finding count, so a gate that also required `verdict == "pass"` could never
    honour either skip key. These tests pin the severities that gate under each
    configuration, and that the strict default is unchanged.
    """

    def setUp(self) -> None:
        self.opsx_plan = load_opsx_plan()
        self.tmp = tempfile.TemporaryDirectory()
        self.repo = Path(self.tmp.name)
        self.cid = "add-thing"
        self.state = {"changes": {}}

    def tearDown(self) -> None:
        self.tmp.cleanup()

    def _cfg(self, **overrides) -> dict:
        cfg = {
            "name": "gate-plan",
            "max_rounds": 5,
            "skip_warning": False,
            "skip_suggestion": False,
        }
        cfg.update(overrides)
        return cfg

    def _review(self, verdict: str, critical: int, warning: int, note: int) -> dict:
        return {
            "status": "reviewed",
            "change": self.cid,
            "round": 1,
            "verdict": verdict,
            "finding_counts": {
                "critical": critical,
                "warning": warning,
                "note": note,
            },
            "summary": "review completed",
            "fix_prompt": "" if verdict == "pass" else "CHANGE: add-thing ...",
            "next_phase": "archive" if verdict == "pass" else "implement",
        }

    def _apply(self, cfg: dict, payload: dict) -> tuple[str, dict]:
        action = self.opsx_plan.apply_review_result(
            self.repo, cfg, self.state, self.cid, payload
        )
        return action, self.opsx_plan.state_mod.rec(self.state, self.cid)

    def test_default_gate_requires_all_counts_zero(self) -> None:
        _, record = self._apply(self._cfg(), self._review("fail", 0, 1, 0))
        self.assertEqual(record["last_result"], "review_failed")
        self.assertEqual(record["phase"], "implement")

    def test_default_gate_passes_on_clean_review(self) -> None:
        _, record = self._apply(self._cfg(), self._review("pass", 0, 0, 0))
        self.assertEqual(record["last_result"], "review_passed")
        self.assertEqual(record["phase"], "archive")

    def test_default_gate_still_defers_to_a_failing_verdict(self) -> None:
        """A zero-count `fail` recommendation must not pass the strict gate."""
        _, record = self._apply(self._cfg(), self._review("fail", 0, 0, 0))
        self.assertEqual(record["last_result"], "review_failed")

    def test_skip_warning_passes_warnings_and_notes(self) -> None:
        _, record = self._apply(
            self._cfg(skip_warning=True), self._review("fail", 0, 2, 1)
        )
        self.assertEqual(record["last_result"], "review_passed")
        self.assertEqual(record["phase"], "archive")
        self.assertEqual(record["latest_fix_prompt"], "")

    def test_skip_warning_still_blocks_on_criticals(self) -> None:
        _, record = self._apply(
            self._cfg(skip_warning=True), self._review("fail", 1, 0, 0)
        )
        self.assertEqual(record["last_result"], "review_failed")
        self.assertEqual(record["phase"], "implement")

    def test_skip_suggestion_passes_notes_only(self) -> None:
        _, record = self._apply(
            self._cfg(skip_suggestion=True), self._review("fail", 0, 0, 3)
        )
        self.assertEqual(record["last_result"], "review_passed")

    def test_skip_suggestion_still_blocks_on_warnings(self) -> None:
        _, record = self._apply(
            self._cfg(skip_suggestion=True), self._review("fail", 0, 1, 0)
        )
        self.assertEqual(record["last_result"], "review_failed")

    def test_skip_gate_still_rejects_an_unexpected_verdict(self) -> None:
        """Relaxing the gate must not stop validating the verdict field."""
        _, record = self._apply(
            self._cfg(skip_warning=True), self._review("maybe", 0, 0, 0)
        )
        self.assertEqual(record["last_result"], "review_invalid")
        self.assertEqual(record["status"], self.opsx_plan.base.FAILED)

    def test_review_history_records_counts_regardless_of_gate(self) -> None:
        self._apply(self._cfg(skip_warning=True), self._review("fail", 0, 2, 1))
        record = self.opsx_plan.state_mod.rec(self.state, self.cid)
        entry = record["history"][-1]
        self.assertEqual(entry["phase"], "review")
        self.assertEqual(
            entry["finding_counts"], {"critical": 0, "warning": 2, "note": 1}
        )
        self.assertEqual(record["last_review"]["verdict"], "fail")

    def test_legacy_payload_without_findings_array_drives_loop_unchanged(self) -> None:
        """3.3/3.4: a review payload with no `findings` array (legacy shape)
        must not fail the change, and must record that the round contributed
        no recurrence evidence."""
        _, record = self._apply(self._cfg(), self._review("fail", 1, 0, 0))
        self.assertEqual(record["last_result"], "review_failed")
        self.assertEqual(record["phase"], "implement")
        entry = record["history"][-1]
        self.assertEqual(entry["findings"], [])


class LocusNormalizationTests(unittest.TestCase):
    """normalize_finding_locus / tracked_files (finding-recurrence-detection change)."""

    def setUp(self) -> None:
        self.opsx_plan = load_opsx_plan()

    def test_varying_path_depth_resolves_to_one_identity(self) -> None:
        """2.4: a short suffix and a deeper suffix of the same unique tracked
        file normalize to the same identity."""
        files = ["orchestrator/agents/executors/result_contract.py"]
        short = self.opsx_plan.normalize_finding_locus("result_contract.py", files)
        deep = self.opsx_plan.normalize_finding_locus(
            "agents/executors/result_contract.py", files
        )
        self.assertEqual(short, deep)
        self.assertEqual(short, "orchestrator/agents/executors/result_contract.py")

    def test_ambiguous_suffix_is_retained_trimmed(self) -> None:
        """2.4: a suffix matching more than one tracked file is retained as-is."""
        files = ["a/widget.py", "b/widget.py"]
        result = self.opsx_plan.normalize_finding_locus("widget.py", files)
        self.assertEqual(result, "widget.py")

    def test_unresolvable_path_is_retained_trimmed(self) -> None:
        """2.4: a path matching no tracked file is retained, not discarded."""
        files = ["a/widget.py"]
        result = self.opsx_plan.normalize_finding_locus("ghost.py", files)
        self.assertEqual(result, "ghost.py")

    def test_exact_symbol_comparison(self) -> None:
        """2.4: the :<symbol> suffix is compared exactly, case included."""
        files = ["src/intake.py"]
        a = self.opsx_plan.normalize_finding_locus("src/intake.py:_apply_outcome", files)
        b = self.opsx_plan.normalize_finding_locus("src/intake.py:_apply_Outcome", files)
        self.assertEqual(a, "src/intake.py:_apply_outcome")
        self.assertNotEqual(a, b)

    def test_trims_whitespace_backticks_and_trailing_punctuation(self) -> None:
        files = ["src/intake.py"]
        result = self.opsx_plan.normalize_finding_locus(
            " `src/intake.py:_apply_outcome`. ", files
        )
        self.assertEqual(result, "src/intake.py:_apply_outcome")

    def test_converts_backslash_separators_to_posix(self) -> None:
        files = ["src/intake.py"]
        result = self.opsx_plan.normalize_finding_locus("src\\intake.py", files)
        self.assertEqual(result, "src/intake.py")

    def test_bare_trailing_colon_collapses_to_path_only(self) -> None:
        files = ["src/intake.py"]
        result = self.opsx_plan.normalize_finding_locus("src/intake.py:", files)
        self.assertEqual(result, "src/intake.py")

    def test_tracked_files_cached_per_repo_for_the_run(self) -> None:
        """2.3: tracked_files() shells out once per repo per process."""
        tmp = tempfile.TemporaryDirectory()
        try:
            repo = Path(tmp.name)
            git(repo, "init")
            (repo / "a.txt").write_text("x\n", encoding="utf-8")
            git(repo, "add", "a.txt")
            git(
                repo,
                "-c", "user.email=test@example.invalid",
                "-c", "user.name=Test User",
                "commit", "-m", "init",
            )
            first = self.opsx_plan.tracked_files(repo)
            (repo / "b.txt").write_text("y\n", encoding="utf-8")
            git(repo, "add", "b.txt")
            second = self.opsx_plan.tracked_files(repo)
            self.assertEqual(
                first, second,
                "tracked_files must be cached per run, not re-shelled per call",
            )
        finally:
            tmp.cleanup()


class FindingRecurrenceDetectionTests(unittest.TestCase):
    """The finding_recurrence_limit ceiling in apply_review_result."""

    def setUp(self) -> None:
        self.opsx_plan = load_opsx_plan()
        self.tmp = tempfile.TemporaryDirectory()
        self.repo = Path(self.tmp.name)
        git(self.repo, "init")
        (self.repo / "src").mkdir()
        (self.repo / "src" / "widget.py").write_text("# widget\n", encoding="utf-8")
        git(self.repo, "add", "src/widget.py")
        git(
            self.repo,
            "-c", "user.email=test@example.invalid",
            "-c", "user.name=Test User",
            "commit", "-m", "init",
        )
        self.cid = "add-thing"
        self.state = {"changes": {}}

    def tearDown(self) -> None:
        self.tmp.cleanup()

    def _cfg(self, **overrides) -> dict:
        cfg = {
            "name": "recurrence-plan",
            "max_rounds": 10,
            "skip_warning": False,
            "skip_suggestion": False,
            "finding_recurrence_limit": 0,
        }
        cfg.update(overrides)
        return cfg

    def _review(self, round_num: int, severity_locus_pairs: list[tuple[str, str]]) -> dict:
        findings = [
            {"severity": sev, "locus": [locus], "statement": "still broken"}
            for sev, locus in severity_locus_pairs
        ]
        counts = {"critical": 0, "warning": 0, "note": 0}
        for sev, _ in severity_locus_pairs:
            counts[sev] += 1
        return {
            "status": "reviewed",
            "change": self.cid,
            "round": round_num,
            "verdict": "fail",
            "finding_counts": counts,
            "summary": "still broken",
            "fix_prompt": "CHANGE: add-thing ...",
            "findings": findings,
            "next_phase": "implement",
        }

    def _apply(self, cfg: dict, payload: dict) -> str:
        # Real dispatch syncs r["max_rounds"] from cfg before every stage
        # (see run_direct_change); direct apply_review_result calls in these
        # tests must do the same or the state-default max_rounds=5 governs.
        r = self.opsx_plan.state_mod.rec(self.state, self.cid)
        r["max_rounds"] = cfg["max_rounds"]
        return self.opsx_plan.apply_review_result(
            self.repo, cfg, self.state, self.cid, payload
        )

    def test_nonconsecutive_recurrence_halts_before_max_rounds(self) -> None:
        """7.1: a locus cited by a blocking finding in rounds 4, 5, 7, and 8
        (never three rounds in a row) halts at round 7 with a ceiling of 3,
        rather than running out the max_rounds budget."""
        cfg = self._cfg(finding_recurrence_limit=3)
        citing_rounds = {4, 5, 7, 8}
        for round_num in range(1, 9):
            r = self.opsx_plan.state_mod.rec(self.state, self.cid)
            self.assertEqual(r["round"], round_num)
            pairs = (
                [("critical", "src/widget.py")]
                if round_num in citing_rounds
                else [("critical", f"src/other{round_num}.py")]
            )
            action = self._apply(cfg, self._review(round_num, pairs))
            record = self.opsx_plan.state_mod.rec(self.state, self.cid)
            if round_num == 7:
                self.assertEqual(action, "stop")
                self.assertEqual(record["last_result"], "finding_recurrence_exceeded")
                self.assertEqual(record["status"], self.opsx_plan.base.FAILED)
                self.assertIn("src/widget.py", record["reason"])
                self.assertIn("4", record["reason"])
                self.assertIn("5", record["reason"])
                self.assertIn("7", record["reason"])
                return
            self.assertEqual(action, "continue")
        self.fail("expected the recurrence ceiling to halt at round 7")

    def test_non_blocking_severity_never_triggers_halt(self) -> None:
        """7.2: a locus cited only by a skipped (non-blocking) severity never
        accumulates recurrence, however many rounds cite it."""
        cfg = self._cfg(finding_recurrence_limit=2, skip_suggestion=True)
        for round_num in range(1, 6):
            pairs = [
                ("critical", f"src/other{round_num}.py"),
                ("note", "src/widget.py"),
            ]
            action = self._apply(cfg, self._review(round_num, pairs))
            self.assertEqual(action, "continue")
        record = self.opsx_plan.state_mod.rec(self.state, self.cid)
        self.assertEqual(record["last_result"], "review_failed")
        self.assertNotEqual(record["status"], self.opsx_plan.base.FAILED)

    def test_passing_review_is_unaffected_by_recurrence_history(self) -> None:
        """Ceiling scenario: a locus reaches the ceiling's round count on the
        very round whose verdict passes the gate — recurrence halting is
        evaluated only after a failing verdict, so a passing round's own
        findings can never trigger it, however they read.

        Round 1 fails, citing the locus once. Round 2's finding_counts are
        all zero (so the gate passes) even though its `findings` array still
        names the same locus, bringing its persisted citation count to the
        `finding_recurrence_limit` of 2 — proving the ceiling never even runs
        the check on a passing round."""
        cfg = self._cfg(finding_recurrence_limit=2)
        action = self._apply(cfg, self._review(1, [("critical", "src/widget.py")]))
        self.assertEqual(action, "continue")
        pass_payload = {
            "status": "reviewed",
            "change": self.cid,
            "round": 2,
            "verdict": "pass",
            "finding_counts": {"critical": 0, "warning": 0, "note": 0},
            "summary": "clean",
            "fix_prompt": "",
            "findings": [
                {"severity": "critical", "locus": ["src/widget.py"], "statement": "was flaky"}
            ],
            "next_phase": "archive",
        }
        action = self._apply(cfg, pass_payload)
        record = self.opsx_plan.state_mod.rec(self.state, self.cid)
        self.assertEqual(action, "continue")
        self.assertEqual(record["last_result"], "review_passed")
        self.assertEqual(record["phase"], "archive")

    def test_disabled_ceiling_never_halts(self) -> None:
        """finding_recurrence_limit = 0 (default) disables recurrence halting
        even when the same locus recurs every round."""
        cfg = self._cfg(finding_recurrence_limit=0, max_rounds=4)
        for round_num in range(1, 5):
            action = self._apply(cfg, self._review(round_num, [("critical", "src/widget.py")]))
        record = self.opsx_plan.state_mod.rec(self.state, self.cid)
        self.assertEqual(record["last_result"], "max_rounds_reached")

    def test_prior_finding_loci_present_and_empty_on_first_round(self) -> None:
        """6.2: PRIOR_FINDING_LOCI is present and explicitly empty for a
        change's first review round."""
        r = self.opsx_plan.state_mod.rec(self.state, self.cid)
        block = self.opsx_plan.build_worker_input(
            self.repo, self._cfg(), self.state, self.cid, stage="review"
        )
        self.assertIn("PRIOR_FINDING_LOCI: ", block)
        lines = {line.split(": ", 1)[0]: line.split(": ", 1)[1] for line in block.splitlines()}
        self.assertEqual(lines["PRIOR_FINDING_LOCI"], "")

    def test_prior_finding_loci_carries_previous_round_blocking_loci(self) -> None:
        """6.1: the second review dispatch carries the first round's
        blocking-finding loci."""
        cfg = self._cfg()
        self._apply(
            cfg,
            self._review(1, [("critical", "src/widget.py"), ("warning", "src/other.py")]),
        )
        block = self.opsx_plan.build_worker_input(
            self.repo, cfg, self.state, self.cid, stage="review"
        )
        lines = {line.split(": ", 1)[0]: line.split(": ", 1)[1] for line in block.splitlines()}
        prior = [loc.strip() for loc in lines["PRIOR_FINDING_LOCI"].split(",")]
        self.assertEqual(set(prior), {"src/widget.py", "src/other.py"})

    def test_prior_finding_loci_absent_from_implement_dispatch(self) -> None:
        """6.1: PRIOR_FINDING_LOCI is a review-dispatch-only field."""
        block = self.opsx_plan.build_worker_input(
            self.repo, self._cfg(), self.state, self.cid, stage="implement"
        )
        self.assertNotIn("PRIOR_FINDING_LOCI", block)

    def test_recurrence_accounting_unaffected_when_reviewer_ignores_prior_loci(self) -> None:
        """6.3: recurrence accounting comes from the orchestrator's own
        normalization regardless of whether PRIOR_FINDING_LOCI is echoed
        back — simulated by never referencing it in review payloads, which
        every other test in this class already does."""
        cfg = self._cfg(finding_recurrence_limit=2)
        for round_num in (1, 2):
            action = self._apply(cfg, self._review(round_num, [("critical", "src/widget.py")]))
        record = self.opsx_plan.state_mod.rec(self.state, self.cid)
        self.assertEqual(action, "stop")
        self.assertEqual(record["last_result"], "finding_recurrence_exceeded")


if __name__ == "__main__":
    unittest.main()
