from __future__ import annotations

import argparse
import importlib.util
import io
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from lib.models import resolver

SCRIPT = Path(__file__).resolve().parents[2] / "orchestrator" / "opsx-plan.py"


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


class LogsCommandTests(unittest.TestCase):
    """Tests for the extracted ``opsx-plan logs`` command handler."""

    def setUp(self) -> None:
        self.opsx_plan = load_opsx_plan()
        self.tmp = tempfile.TemporaryDirectory()
        self.repo = Path(self.tmp.name)
        git(self.repo, "init")
        (self.repo / "tracked.txt").write_text("base\n", encoding="utf-8")
        git(self.repo, "add", "tracked.txt")
        git(self.repo, "-c", "user.email=test@example.invalid",
            "-c", "user.name=Test User", "commit", "-m", "init")
        self.cid = "add-logs-test"
        self.plan_name = "run-add-logs-test"
        self.plan_path = self.repo / "plan.toml"
        self.plan_path.write_text(
            f'[plan]\nname = "{self.plan_name}"\nadapter = "opencode"\n\n'
            f'[[changes]]\nid = "{self.cid}"\n', encoding="utf-8"
        )
        self.log_dir = self.repo / ".opsx-plan" / "logs"
        self.log_dir.mkdir(parents=True)
        self._model_home = tempfile.TemporaryDirectory()
        self.addCleanup(self._model_home.cleanup)
        self._models_patch = mock.patch.object(
            resolver, "USER_CONFIG_PATH", Path(self._model_home.name) / "models.toml"
        )
        self._models_patch.start()
        self.addCleanup(self._models_patch.stop)
        self._env_patch = mock.patch.dict(os.environ, {
            "OPSX_CONTROLLER_MODEL": "test-provider/test-controller",
            "OPSX_IMPLEMENTER_MODEL": "test-provider/test-implementer",
            "OPSX_REVIEWER_MODEL": "test-provider/test-reviewer",
            "OPSX_ARCHIVER_MODEL": "test-provider/test-archiver",
        })
        self._env_patch.start()
        self.addCleanup(self._env_patch.stop)

    def tearDown(self) -> None:
        self.tmp.cleanup()

    def _make_log(self, name: str, body: str) -> Path:
        path = self.log_dir / name
        path.write_text(body, encoding="utf-8")
        return path

    def _args(self, list_mode: bool = False) -> argparse.Namespace:
        return argparse.Namespace(repo=str(self.repo), plan=str(self.plan_path),
                                  change=None, stage=None, list=list_mode, follow=False)

    def test_cmd_logs_list_mode_excludes_out_of_plan_logs(self) -> None:
        self._make_log(f"{self.cid}.implement.r1.1.log", "plan log\n")
        self._make_log("other-plan-change.review.r1.1.log", "foreign log\n")
        with mock.patch("sys.stdout", io.StringIO()):
            self.assertEqual(self.opsx_plan.cmd_logs.cmd_logs(self._args(True)), 0)

    def test_cmd_logs_default_excludes_out_of_plan_logs(self) -> None:
        self._make_log("foreign-change.implement.r1.1.log", "foreign\n")
        self.assertEqual(self.opsx_plan.cmd_logs.cmd_logs(self._args()), 1)

    def test_cmd_logs_exits_nonzero_for_missing_log(self) -> None:
        self.assertEqual(self.opsx_plan.cmd_logs.cmd_logs(self._args()), 1)

    def test_logs_subcommand_routes_to_cmd_logs(self) -> None:
        with mock.patch.object(self.opsx_plan.cmd_logs, "cmd_logs", return_value=42) as command, \
             mock.patch.object(self.opsx_plan.sys, "argv",
                               ["opsx-plan", "--repo", str(self.repo), "logs", str(self.plan_path)]):
            self.assertEqual(self.opsx_plan.main(), 42)
        command.assert_called_once()

    def test_logs_list_mode_cli(self) -> None:
        self._make_log(f"{self.cid}.implement.r1.1.log", "impl\n")
        with mock.patch("sys.stdout", io.StringIO()):
            self.assertEqual(self.opsx_plan.cmd_logs.cmd_logs(self._args(True)), 0)
