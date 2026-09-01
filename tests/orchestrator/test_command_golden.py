from __future__ import annotations

import argparse
import importlib.util
import io
import os
import subprocess
import sys
import tempfile
import unittest
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path
from unittest import mock

from lib.models import resolver

SCRIPT = Path(__file__).resolve().parents[2] / "orchestrator" / "opsx-plan.py"
GOLDEN = Path(__file__).with_name("golden")


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


class GoldenCommandOutputTests(unittest.TestCase):
    """Byte-compare stable command output against the extraction baseline."""

    def setUp(self) -> None:
        self.opsx_plan = load_opsx_plan()
        self.tmp = tempfile.TemporaryDirectory()
        self.repo = Path(self.tmp.name)
        git(self.repo, "init")
        (self.repo / "tracked.txt").write_text("base\n", encoding="utf-8")
        git(self.repo, "add", "tracked.txt")
        git(self.repo, "-c", "user.email=test@example.invalid",
            "-c", "user.name=Test User", "commit", "-m", "init")
        (self.repo / ".opsx-plan" / "logs").mkdir(parents=True)
        (self.repo / ".opsx-plan" / "logs" / "golden-change.implement.r1.1.log").write_text(
            "golden log line\n", encoding="utf-8"
        )
        self.plan = self.repo / "plan.toml"
        self.plan.write_text(
            '[plan]\nname = "golden-plan"\nadapter = "opencode"\n\n'
            '[[changes]]\nid = "golden-change"\n', encoding="utf-8"
        )
        self._model_patch = mock.patch.object(
            resolver, "USER_CONFIG_PATH", self.repo / "models.toml"
        )
        self._model_patch.start()
        self.addCleanup(self._model_patch.stop)
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

    def _expected(self, name: str) -> str:
        return (GOLDEN / name).read_text(encoding="utf-8")

    def _assert_golden(self, name: str, output: str) -> None:
        normalized = output.replace(str(self.repo), "<REPO>")
        self.assertEqual(normalized, self._expected(name))

    def test_status_output_matches_golden(self) -> None:
        args = argparse.Namespace(repo=str(self.repo), plan=str(self.plan))
        out = io.StringIO()
        with mock.patch.object(self.opsx_plan, "reconcile"), \
             mock.patch.object(self.opsx_plan, "validate_dsh_state_files"), \
             mock.patch.object(self.opsx_plan, "sync_direct_worker_state"), \
             redirect_stdout(out):
            self.assertEqual(self.opsx_plan.cmd_status.cmd_status(args), 0)
        self._assert_golden("status.txt", out.getvalue())

    def test_logs_output_matches_golden(self) -> None:
        args = argparse.Namespace(repo=str(self.repo), plan=str(self.plan),
                                  change=None, stage=None, list=True, follow=False)
        out = io.StringIO()
        with redirect_stdout(out):
            self.assertEqual(self.opsx_plan.cmd_logs.cmd_logs(args), 0)
        self._assert_golden("logs.txt", out.getvalue())

    def test_doctor_output_matches_golden(self) -> None:
        args = argparse.Namespace(repo=str(self.repo), plan=None)
        out = io.StringIO()
        with mock.patch.object(self.opsx_plan, "run_doctor_checks", return_value=0), \
             redirect_stdout(out):
            self.assertEqual(self.opsx_plan.cmd_doctor.cmd_doctor(args), 0)
        self._assert_golden("doctor.txt", out.getvalue())

    def test_models_show_output_matches_golden(self) -> None:
        args = argparse.Namespace(repo=str(self.repo), adapter="opencode")
        out = io.StringIO()
        with redirect_stdout(out):
            self.assertEqual(self.opsx_plan.cmd_models.cmd_models_show(args), 0)
        self._assert_golden("models-show.txt", out.getvalue())

    def test_approve_help_output_matches_golden(self) -> None:
        out = io.StringIO()
        with mock.patch.object(self.opsx_plan.sys, "argv", ["opsx-plan", "approve", "--help"]), \
             redirect_stdout(out), self.assertRaises(SystemExit) as raised:
            self.opsx_plan.main()
        self.assertEqual(raised.exception.code, 0)
        self._assert_golden("approve-help.txt", out.getvalue())

    def test_use_output_matches_golden(self) -> None:
        args = argparse.Namespace(repo=str(self.repo), plan="plan.toml")
        out = io.StringIO()
        with mock.patch.object(self.opsx_plan.base, "log"), redirect_stdout(out):
            self.assertEqual(self.opsx_plan.cmd_use.cmd_use(args), 0)
        self._assert_golden("use.txt", out.getvalue())
