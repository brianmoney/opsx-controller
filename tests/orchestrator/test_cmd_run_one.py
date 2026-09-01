from __future__ import annotations

import argparse
import importlib.util
import io
import json
import os
import re
import shlex
import subprocess
import sys
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
    sys.modules["opsx_plan"] = module
    spec.loader.exec_module(module)
    return module


def git(repo: Path, *args: str) -> None:
    subprocess.run(["git", *args], cwd=repo, check=True, capture_output=True, text=True)


def apply_completed_tasks(repo: Path, cid: str, completed_tasks: list[str] | None) -> None:
    """Mirror a real implementer's checkbox edits on the change tasks file.

    Stage harnesses call this for each simulated `implemented` round so the
    controller's task-completeness gate sees the on-disk ground truth that a
    worker would have produced. Only the task ids the worker claims are
    checked off, so a payload that reports `implemented` while leaving
    automatable tasks unchecked still trips the gate.
    """
    tasks_path = repo / "openspec" / "changes" / cid / "tasks.md"
    if not tasks_path.is_file():
        return
    lines = tasks_path.read_text(encoding="utf-8").splitlines()
    changed = False
    for i, line in enumerate(lines):
        for tid in completed_tasks or []:
            if tid and line.startswith(f"- [ ] {tid}"):
                lines[i] = line.replace("- [ ]", "- [x]", 1)
                changed = True
                break
    if changed:
        tasks_path.write_text("\n".join(lines), encoding="utf-8")


def extract_result_json(body: str) -> dict:
    """Best-effort parse of a stage log body into its result dict.

    Mirrors ``parse_stage_json``'s tolerance for transcript noise by using the
    last line that parses as a JSON object, so harnesses can simulate checkbox
    edits for noisy logs too.
    """
    for line in reversed(body.splitlines()):
        stripped = line.strip()
        if stripped.startswith("{") and stripped.endswith("}"):
            try:
                return json.loads(stripped)
            except ValueError:
                continue
    try:
        return json.loads(body)
    except ValueError:
        return {}

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
        rc = self.opsx_plan.cmd_run_one.cmd_run_one(args)
        self.assertEqual(rc, 2)

    def test_cmd_run_one_rejects_unauthored_change(self) -> None:
        cdir = self.repo / "openspec" / "changes" / "bare"
        cdir.mkdir(parents=True)
        args = argparse.Namespace(repo=str(self.repo), change="bare")
        rc = self.opsx_plan.cmd_run_one.cmd_run_one(args)
        self.assertEqual(rc, 2)

    def test_cmd_run_one_rejects_dirty_tracked_worktree(self) -> None:
        self.write_authored_change("add-dirty")
        (self.repo / "tracked.txt").write_text("dirty\n", encoding="utf-8")

        args = argparse.Namespace(repo=str(self.repo), change="add-dirty")
        stderr = io.StringIO()

        with mock.patch.object(self.opsx_plan, "run_direct_change") as run_direct_change, mock.patch("sys.stderr", stderr):
            rc = self.opsx_plan.cmd_run_one.cmd_run_one(args)

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
            if stage == "implement":
                parsed = extract_result_json(body)
                if parsed.get("status") == "implemented":
                    apply_completed_tasks(repo, cid, parsed.get("completed_tasks"))
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
            if stage == "implement":
                parsed = extract_result_json(body)
                if parsed.get("status") == "implemented":
                    apply_completed_tasks(repo, cid, parsed.get("completed_tasks"))
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
                        "progress_made": True, "completed_tasks": ["1.1", "1.2"],
                        "remaining_tasks": [], "task_counts": {"complete": 2, "total": 2},
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
                        "progress_made": True, "completed_tasks": ["1.1", "1.2"],
                        "remaining_tasks": [], "task_counts": {"complete": 2, "total": 2},
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
                        "progress_made": True, "completed_tasks": ["1.1", "1.2"],
                        "remaining_tasks": [], "task_counts": {"complete": 2, "total": 2},
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
                        "progress_made": False, "completed_tasks": ["1.1", "1.2"],
                        "remaining_tasks": [], "task_counts": {"complete": 2, "total": 2},
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
                        "progress_made": True, "completed_tasks": ["1.1", "1.2"],
                        "remaining_tasks": [], "task_counts": {"complete": 2, "total": 2},
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
                    "progress_made": True, "completed_tasks": ["1.1", "1.2"],
                    "remaining_tasks": [], "task_counts": {"complete": 2, "total": 2},
                    "files_touched": [], "known_change_files": [],
                    "summary": f"impl r{round_num}",
                }) + "\n"
                apply_completed_tasks(repo, cid, ["1.1", "1.2"])
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
                rc = self.opsx_plan.cmd_run_one.cmd_run_one(args)
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
            rc = self.opsx_plan.cmd_run_one.cmd_run_one(args)

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
            rc = self.opsx_plan.cmd_run_one.cmd_run_one(args)

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


class RunOneCommandTests(unittest.TestCase):
    """opsx-run executable dispatch and single-change run-one command."""

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
            self.opsx_plan.cmd_run_one, "cmd_run_one", side_effect=fake_cmd_run_one
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
