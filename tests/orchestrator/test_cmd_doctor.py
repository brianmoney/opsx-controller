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


class DoctorCommandTests(unittest.TestCase):
    """Tests for the extracted ``opsx-plan doctor`` command handler."""

    def setUp(self) -> None:
        self.opsx_plan = load_opsx_plan()
        self.tmp = tempfile.TemporaryDirectory()
        self.repo = Path(self.tmp.name)
        git(self.repo, "init")
        (self.repo / "tracked.txt").write_text("base\n", encoding="utf-8")
        git(self.repo, "add", "tracked.txt")
        git(self.repo, "-c", "user.email=test@example.invalid",
            "-c", "user.name=Test User", "commit", "-m", "init")
        self._models_patch = mock.patch.object(
            resolver, "USER_CONFIG_PATH", self.repo / "unused-home" / "models.toml"
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

    def _write_plan_toml(self, name: str = "doctor-test-plan") -> Path:
        path = self.repo / f"{name}.toml"
        path.write_text(
            f'[plan]\nname = "{name}"\nadapter = "opencode"\n'
            "timeout_minutes = 1\nmax_rounds = 5\n"
            "require_clean_tracked = false\n"
            '[[changes]]\nid = "ch-doctor"\n', encoding="utf-8"
        )
        return path

    def test_cmd_doctor_with_no_plan_runs_plan_independent_checks(self) -> None:
        args = argparse.Namespace(repo=str(self.repo), plan=None)
        stdout = io.StringIO()
        saved_env = {v: os.environ.pop(v, None) for v in self.opsx_plan.ROLE_ENV.values()}
        try:
            with mock.patch("sys.stdout", stdout):
                rc = self.opsx_plan.cmd_doctor.cmd_doctor(args)
        finally:
            for var, value in saved_env.items():
                if value is not None:
                    os.environ[var] = value
        self.assertIn("(none", stdout.getvalue())
        self.assertEqual(rc, 1)

    def test_cmd_doctor_with_explicit_plan_loads_plan_checks(self) -> None:
        plan_path = self._write_plan_toml()
        plan_src = str(plan_path.relative_to(self.repo))
        args = argparse.Namespace(repo=str(self.repo), plan=plan_src)
        stdout = io.StringIO()
        with mock.patch("sys.stdout", stdout):
            rc = self.opsx_plan.cmd_doctor.cmd_doctor(args)
        output = stdout.getvalue()
        self.assertIn("opsx-plan doctor", output)
        self.assertIn(plan_src, output)
        self.assertIn("Plan loads successfully", output)
        self.assertIn("Model roles resolve for the target adapter", output)
        self.assertIsNotNone(rc)
        self.assertIn("controller", output)
        self.assertIn("ambient environment", output)

    def test_cmd_doctor_with_missing_explicit_plan_exits_nonzero(self) -> None:
        args = argparse.Namespace(repo=str(self.repo), plan="nonexistent.toml")
        self.assertEqual(self.opsx_plan.cmd_doctor.cmd_doctor(args), 2)

    def test_cmd_doctor_with_stale_active_plan_warns_and_continues(self) -> None:
        self.opsx_plan.write_active_plan(self.repo, "gone-plan.toml")
        args = argparse.Namespace(repo=str(self.repo), plan=None)
        stdout, stderr = io.StringIO(), io.StringIO()
        with mock.patch("sys.stdout", stdout), mock.patch("sys.stderr", stderr):
            self.opsx_plan.cmd_doctor.cmd_doctor(args)
        self.assertIn("active plan pointer references missing file", stderr.getvalue())
        self.assertIn("gone-plan.toml", stderr.getvalue())
        self.assertIn("(none", stdout.getvalue())

    def test_cmd_doctor_with_unloadable_active_plan_exits_nonzero(self) -> None:
        bad_plan = self.repo / "bad-active.toml"
        bad_plan.write_text("not valid toml {{{", encoding="utf-8")
        self.opsx_plan.write_active_plan(self.repo, "bad-active.toml")
        args = argparse.Namespace(repo=str(self.repo), plan=None)
        stderr = io.StringIO()
        with mock.patch("sys.stderr", stderr):
            rc = self.opsx_plan.cmd_doctor.cmd_doctor(args)
        self.assertEqual(rc, 2)
        self.assertIn("cannot load plan", stderr.getvalue())
        self.assertIn("bad-active.toml", stderr.getvalue())

    def test_doctor_planless_claude_selection(self) -> None:
        args = argparse.Namespace(repo=str(self.repo), plan=None, adapter="claude-code")
        with mock.patch.object(self.opsx_plan, "run_doctor_checks", return_value=0) as run:
            rc = self.opsx_plan.cmd_doctor.cmd_doctor(args)
        self.assertEqual(rc, 0)
        self.assertEqual(run.call_args[0][2], "claude-code")

    def test_doctor_plan_adapter_overrides_flag(self) -> None:
        plan_path = self._write_plan_toml("override-test")
        args = argparse.Namespace(
            repo=str(self.repo), plan=str(plan_path.relative_to(self.repo)), adapter="claude-code"
        )
        with mock.patch.object(self.opsx_plan, "run_doctor_checks", return_value=0) as run:
            rc = self.opsx_plan.cmd_doctor.cmd_doctor(args)
        self.assertEqual(rc, 0)
        self.assertEqual(run.call_args[0][2], "opencode")

    def test_doctor_defaults_to_opencode_without_flag_or_plan(self) -> None:
        args = argparse.Namespace(repo=str(self.repo), plan=None)
        with mock.patch.object(self.opsx_plan, "run_doctor_checks", return_value=0) as run:
            rc = self.opsx_plan.cmd_doctor.cmd_doctor(args)
        self.assertEqual(rc, 0)
        self.assertEqual(run.call_args[0][2], "opencode")
