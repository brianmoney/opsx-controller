from __future__ import annotations

import argparse
import ast
import importlib.util
import sys
import inspect
import io
import json
import os
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from lib.models import resolver
from lib.orchestrator import doctor as doctor_mod
from lib.orchestrator import state as state_mod
from lib.orchestrator import base as base_mod
from lib.orchestrator import groundtruth as groundtruth_mod
from lib.orchestrator import telemetry as telemetry_mod

_SCRIPT = Path(__file__).resolve().parents[2] / "orchestrator" / "opsx-plan.py"

_MODEL_HOME: tempfile.TemporaryDirectory | None = None
_MODEL_CONFIG_PATCH = None
_MODEL_ENV_PATCH = None


def setUpModule() -> None:
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


def load_opsx_plan():
    spec = importlib.util.spec_from_file_location("opsx_plan", _SCRIPT)
    assert spec is not None
    assert spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules["opsx_plan"] = module
    spec.loader.exec_module(module)
    return module


class DoctorPreflightTests(unittest.TestCase):
    """Tests for ``opsx-plan doctor`` preflight checks and run-start warnings
    (tasks 4.1, 4.2, 4.3)."""

    def setUp(self) -> None:
        self.opsx_plan = load_opsx_plan()
        self.tmp = tempfile.TemporaryDirectory()
        self.repo = Path(self.tmp.name)
        git(self.repo, "init")
        (self.repo / "tracked.txt").write_text("base\n", encoding="utf-8")
        git(self.repo, "add", "tracked.txt")
        git(
            self.repo,
            "-c", "user.email=test@example.invalid",
            "-c", "user.name=Test User",
            "commit", "-m", "init",
        )
        # Isolate model resolution from whatever the real machine's home
        # directory happens to contain, so these tests are hermetic.
        from lib.models import resolver as _resolver
        self._models_patch = mock.patch.object(
            _resolver, "USER_CONFIG_PATH", Path(self.tmp.name) / "unused-home" / "models.toml"
        )
        self._models_patch.start()
        self.addCleanup(self._models_patch.stop)

    def tearDown(self) -> None:
        self.tmp.cleanup()

    def _write_plan_toml(self, name: str = "doctor-test-plan",
                         delivery: str = "") -> Path:
        """Write a minimal plan TOML and return its absolute path."""
        plan_content = (
            f'[plan]\nname = "{name}"\nadapter = "opencode"\n'
            f'timeout_minutes = 1\nmax_rounds = 5\n'
            f'require_clean_tracked = false\n'
        )
        if delivery:
            plan_content += f'delivery = "{delivery}"\n'
        plan_content += (
            '[[changes]]\nid = "ch-doctor"\n'
        )
        p = self.repo / f"{name}.toml"
        p.write_text(plan_content, encoding="utf-8")
        return p

    # -- 4.1: failing check classes ---------------------------------------

    def test_check_model_resolution_missing_reports_failures(self) -> None:
        """Unresolved OPSX_*_MODEL roles should produce a failing check."""
        saved_env = {}
        for v in self.opsx_plan.ROLE_ENV.values():
            saved_env[v] = os.environ.pop(v, None)
        try:
            passed, label, remediation = doctor_mod._check_model_resolution(
                self.repo, "opencode"
            )
            self.assertFalse(passed)
            self.assertIn("Unresolved role", remediation)
        finally:
            for v, val in saved_env.items():
                if val is not None:
                    os.environ[v] = val
                elif v in os.environ:
                    del os.environ[v]

    def test_check_model_resolution_passes_when_all_set(self) -> None:
        """When all OPSX_*_MODEL vars resolve, the check passes."""
        saved_env = {}
        for v in self.opsx_plan.ROLE_ENV.values():
            saved_env[v] = os.environ.get(v)
            os.environ[v] = "provider/test-model-value"
        try:
            passed, label, remediation = doctor_mod._check_model_resolution(
                self.repo, "opencode"
            )
            self.assertTrue(passed, f"check failed: {remediation}")
        finally:
            for v, val in saved_env.items():
                if val is not None:
                    os.environ[v] = val
                elif v in os.environ:
                    del os.environ[v]

    def test_check_model_identifier_syntax_fails_on_provider_prefix_for_claude_code(self) -> None:
        """A provider-prefixed identifier under claude-code fails the syntax check."""
        saved_env = {}
        for v in self.opsx_plan.ROLE_ENV.values():
            saved_env[v] = os.environ.get(v)
            os.environ[v] = "deepseek/deepseek-v4-pro"
        try:
            passed, label, remediation = doctor_mod._check_model_identifier_syntax(
                self.repo, "claude-code"
            )
            self.assertFalse(passed)
            self.assertIn("provider-prefixed", remediation)
        finally:
            for v, val in saved_env.items():
                if val is not None:
                    os.environ[v] = val
                elif v in os.environ:
                    del os.environ[v]

    def test_check_model_identifier_syntax_fails_on_bare_identifier_for_opencode(self) -> None:
        """A bare identifier under opencode fails the syntax check."""
        saved_env = {}
        for v in self.opsx_plan.ROLE_ENV.values():
            saved_env[v] = os.environ.get(v)
            os.environ[v] = "gpt-5.4"
        try:
            passed, label, remediation = doctor_mod._check_model_identifier_syntax(
                self.repo, "opencode"
            )
            self.assertFalse(passed)
            self.assertIn("provider/", remediation)
        finally:
            for v, val in saved_env.items():
                if val is not None:
                    os.environ[v] = val
                elif v in os.environ:
                    del os.environ[v]

    def test_check_model_identifier_syntax_passes_for_matching_syntax(self) -> None:
        """A correctly-shaped identifier passes the syntax check."""
        saved_env = {}
        for v in self.opsx_plan.ROLE_ENV.values():
            saved_env[v] = os.environ.get(v)
            os.environ[v] = "provider/test-model-value"
        try:
            passed, label, remediation = doctor_mod._check_model_identifier_syntax(
                self.repo, "opencode"
            )
            self.assertTrue(passed, f"check failed: {remediation}")
        finally:
            for v, val in saved_env.items():
                if val is not None:
                    os.environ[v] = val
                elif v in os.environ:
                    del os.environ[v]

    def test_check_tracked_bytecode_no_false_positives_on_clean_tree(self) -> None:
        """A clean tree without bytecode should pass."""
        passed, label, remediation = doctor_mod._check_tracked_bytecode(self.repo)
        self.assertTrue(passed, f"unexpected failure: {remediation}")

    def test_check_tracked_bytecode_detects_tracked_pyc(self) -> None:
        """Tracked .pyc files should be detected."""
        pyc = self.repo / "cached.pyc"
        pyc.write_text("fake", encoding="utf-8")
        git(self.repo, "add", "cached.pyc")
        git(
            self.repo,
            "-c", "user.email=test@example.invalid",
            "-c", "user.name=Test User",
            "commit", "-m", "add pyc",
        )
        passed, label, remediation = doctor_mod._check_tracked_bytecode(self.repo)
        self.assertFalse(passed)
        self.assertIn("cached.pyc", remediation)

    def test_check_tracked_tree_clean_passes_on_clean_tree(self) -> None:
        """A clean tree should pass the check."""
        passed, label, remediation = doctor_mod._check_tracked_tree_clean(self.repo)
        self.assertTrue(passed, f"unexpected failure: {remediation}")

    def test_check_tracked_tree_clean_detects_dirty_tree(self) -> None:
        """A dirty tracked tree should fail the check."""
        (self.repo / "tracked.txt").write_text("modified\n", encoding="utf-8")
        passed, label, remediation = doctor_mod._check_tracked_tree_clean(self.repo)
        self.assertFalse(passed)
        self.assertIn("uncommitted", remediation)

    def test_check_plan_loads_passes_when_plan_is_valid(self) -> None:
        """A valid plan should load successfully."""
        plan_path = self._write_plan_toml()
        plan_src = str(plan_path.relative_to(self.repo))
        passed, label, remediation = doctor_mod._check_plan_loads(self.repo, plan_src)
        self.assertTrue(passed, f"unexpected failure: {remediation}")

    def test_check_plan_loads_fails_when_plan_is_invalid(self) -> None:
        """An invalid plan should fail to load with a clear message."""
        path = self.repo / "bad.toml"
        path.write_text("not valid toml [[[", encoding="utf-8")
        plan_src = str(path.relative_to(self.repo))
        passed, label, remediation = doctor_mod._check_plan_loads(self.repo, plan_src)
        self.assertFalse(passed)
        self.assertIn("Plan load failed", remediation)

    def test_check_plan_loads_skips_when_plan_src_is_none(self) -> None:
        """No plan should be treated as a pass (skip)."""
        passed, label, remediation = doctor_mod._check_plan_loads(self.repo, None)
        self.assertTrue(passed)

    def test_check_pr_delivery_skips_when_no_plan(self) -> None:
        """When plan_src is None, PR check should skip (pass)."""
        passed, label, remediation = doctor_mod._check_pr_delivery(self.repo, None)
        self.assertTrue(passed)

    def test_check_pr_delivery_fails_when_plan_load_fails(self) -> None:
        """When the plan TOML cannot be loaded, PR check must FAIL, not pass."""
        path = self.repo / "bad-pr.toml"
        path.write_text("broken toml [[{", encoding="utf-8")
        plan_src = str(path.relative_to(self.repo))
        passed, label, remediation = doctor_mod._check_pr_delivery(self.repo, plan_src)
        self.assertFalse(passed)
        self.assertIn("Plan failed to load", remediation)

    def test_check_pr_delivery_passes_when_delivery_is_not_pr(self) -> None:
        """When delivery is not pull-request, the check passes."""
        plan_path = self._write_plan_toml(delivery="none")
        plan_src = str(plan_path.relative_to(self.repo))
        passed, label, remediation = doctor_mod._check_pr_delivery(self.repo, plan_src)
        self.assertTrue(passed)

    def test_check_pr_delivery_fails_when_gh_missing(self) -> None:
        """When delivery is pull-request but gh is missing, check fails."""
        plan_path = self._write_plan_toml(delivery="pull-request")
        plan_src = str(plan_path.relative_to(self.repo))
        with mock.patch("shutil.which", return_value=None):
            passed, label, remediation = doctor_mod._check_pr_delivery(self.repo, plan_src)
        self.assertFalse(passed)
        self.assertIn("gh", remediation.lower())

    def test_check_pr_delivery_fails_when_no_git_remote(self) -> None:
        """When delivery is pull-request, gh is present but no remote, check fails."""
        plan_path = self._write_plan_toml(delivery="pull-request")
        plan_src = str(plan_path.relative_to(self.repo))
        # gh on PATH, but we have no remote (bare init)
        with mock.patch("shutil.which", return_value="/usr/bin/gh"):
            passed, label, remediation = doctor_mod._check_pr_delivery(self.repo, plan_src)
        self.assertFalse(passed)
        self.assertIn("No git remote", remediation)

    # -- 4.1 (continued): missing failure-path tests for stale install,
    #    missing openspec, and missing adapter client -----------------------

    def test_check_stale_install_fails_when_hashes_differ(self) -> None:
        """When the installed copy content differs from the repo copy, the
        check must fail with a stale-install message."""
        import hashlib

        repo_copy = self.repo / "orchestrator" / "opsx-plan.py"
        repo_copy.parent.mkdir(parents=True)
        repo_copy.write_text("repo content", encoding="utf-8")

        fake_home = Path(self.tmp.name) / "fake-home"
        fake_home.mkdir(parents=True, exist_ok=True)
        installed = fake_home / ".local" / "bin" / "opsx-plan"
        installed.parent.mkdir(parents=True, exist_ok=True)
        installed.write_text("different installed content", encoding="utf-8")

        with mock.patch.object(Path, "home", return_value=fake_home):
            passed, label, remediation = self.opsx_plan._check_stale_install(self.repo)

        self.assertFalse(passed)
        self.assertIn("stale", remediation.lower())

    def test_check_stale_install_fails_when_installed_missing(self) -> None:
        """When the installed copy does not exist at all, the check must fail."""
        repo_copy = self.repo / "orchestrator" / "opsx-plan.py"
        repo_copy.parent.mkdir(parents=True)
        repo_copy.write_text("repo content", encoding="utf-8")

        fake_home = Path(self.tmp.name) / "fake-home-missing"
        fake_home.mkdir(parents=True, exist_ok=True)

        with mock.patch.object(Path, "home", return_value=fake_home):
            passed, label, remediation = self.opsx_plan._check_stale_install(self.repo)

        self.assertFalse(passed)
        self.assertIn("not found", remediation.lower())

    def test_check_openspec_on_path_fails_when_openspec_missing(self) -> None:
        """When openspec is not resolvable repo-locally or on PATH, the check
        must fail with an initialize-OpenSpec hint."""
        with mock.patch("shutil.which", return_value=None):
            passed, label, remediation = doctor_mod._check_openspec_on_path(self.repo)

        self.assertFalse(passed)
        self.assertIn("npx openspec@latest init", remediation)

    def test_check_openspec_on_path_passes_when_found(self) -> None:
        """When openspec is on PATH, the check must pass."""
        with mock.patch("shutil.which", return_value="/usr/bin/openspec"):
            passed, label, remediation = doctor_mod._check_openspec_on_path(self.repo)

        self.assertTrue(passed, f"unexpected failure: {remediation}")

    def test_check_openspec_on_path_passes_with_repo_local_openspec(self) -> None:
        """A repo-local node_modules/.bin/openspec passes even when no global
        openspec is on PATH."""
        bin_dir = self.repo / "node_modules" / ".bin"
        bin_dir.mkdir(parents=True)
        (bin_dir / "openspec").write_text("#!/bin/sh\n", encoding="utf-8")
        with mock.patch("shutil.which", return_value=None):
            passed, label, remediation = doctor_mod._check_openspec_on_path(self.repo)

        self.assertTrue(passed, f"unexpected failure: {remediation}")

    def test_resolve_openspec_binary_prefers_repo_local_over_path(self) -> None:
        """A repo-local OpenSpec binary wins when a global one also exists."""
        bin_dir = self.repo / "node_modules" / ".bin"
        bin_dir.mkdir(parents=True)
        local = bin_dir / "openspec"
        local.write_text("#!/bin/sh\n", encoding="utf-8")

        with mock.patch("shutil.which", return_value="/usr/bin/openspec"):
            resolved = doctor_mod._resolve_openspec_binary(self.repo)

        self.assertEqual(resolved, str(local))

    def test_check_openspec_initialized_fails_when_config_missing(self) -> None:
        """A repo without openspec/config.yaml must fail the check with an
        openspec init hint."""
        with mock.patch("shutil.which", return_value="/usr/bin/openspec"), \
             mock.patch.object(doctor_mod, "_installed_openspec_version", return_value="1.9.0"):
            passed, label, remediation = doctor_mod._check_openspec_initialized(self.repo)

        self.assertFalse(passed)
        self.assertIn("openspec init", remediation)
        self.assertIn("1.9.0", remediation)

    def test_check_openspec_initialized_passes_when_healthy_root(self) -> None:
        """A repo with openspec/config.yaml and a resolvable root passes."""
        (self.repo / "openspec" / "config.yaml").parent.mkdir(parents=True)
        (self.repo / "openspec" / "config.yaml").write_text("schema: spec-driven\n", encoding="utf-8")
        payload = json.dumps({
            "changes": [],
            "root": {"path": str(self.repo), "source": "nearest"},
            "status": [],
        })
        with mock.patch("shutil.which", return_value="/usr/bin/openspec"), \
             mock.patch.object(doctor_mod, "_installed_openspec_version", return_value="1.9.0"), \
             mock.patch.object(doctor_mod.subprocess, "run",
                               return_value=mock.Mock(returncode=0, stdout=payload, stderr="")):
            passed, label, remediation = doctor_mod._check_openspec_initialized(self.repo)

        self.assertTrue(passed, f"unexpected failure: {remediation}")

    def test_check_openspec_initialized_fails_when_root_unresolvable(self) -> None:
        """Even with config present, an unresolvable root must fail."""
        (self.repo / "openspec" / "config.yaml").parent.mkdir(parents=True)
        (self.repo / "openspec" / "config.yaml").write_text("schema: spec-driven\n", encoding="utf-8")
        payload = json.dumps({
            "changes": [],
            "root": None,
            "status": [{"severity": "error", "code": "no_openspec_root",
                        "message": "No OpenSpec root found from the current directory.",
                        "target": "openspec.root",
                        "fix": "Run openspec init to create a root here."}],
        })
        with mock.patch("shutil.which", return_value="/usr/bin/openspec"), \
             mock.patch.object(doctor_mod, "_installed_openspec_version", return_value="1.9.0"), \
             mock.patch.object(doctor_mod.subprocess, "run",
                               return_value=mock.Mock(returncode=0, stdout=payload, stderr="")):
            passed, label, remediation = doctor_mod._check_openspec_initialized(self.repo)

        self.assertFalse(passed)
        self.assertIn("openspec init", remediation)
        self.assertIn("No OpenSpec root", remediation)

    def test_check_openspec_initialized_fails_when_openspec_missing(self) -> None:
        """When openspec is not on PATH the check must fail with a hint."""
        with mock.patch("shutil.which", return_value=None):
            passed, label, remediation = doctor_mod._check_openspec_initialized(self.repo)

        self.assertFalse(passed)
        self.assertIn("openspec init", remediation)

    def test_installed_openspec_version_parses(self) -> None:
        """The version parser extracts X.Y.Z from --version output."""
        res = mock.Mock(returncode=0, stdout="1.9.0\n", stderr="")
        with mock.patch("shutil.which", return_value="/usr/bin/openspec"), \
             mock.patch.object(doctor_mod.subprocess, "run", return_value=res):
            self.assertEqual(doctor_mod._installed_openspec_version(self.repo), "1.9.0")

    def test_installed_openspec_version_none_on_failure(self) -> None:
        """A non-zero --version probe yields None."""
        res = mock.Mock(returncode=1, stdout="", stderr="boom")
        with mock.patch("shutil.which", return_value="/usr/bin/openspec"), \
             mock.patch.object(doctor_mod.subprocess, "run", return_value=res):
            self.assertIsNone(doctor_mod._installed_openspec_version(self.repo))

    def test_check_adapter_client_on_path_fails_when_client_missing(self) -> None:
        """When the adapter client executable is not on PATH, the check must
        fail with an install hint."""
        with mock.patch("shutil.which", return_value=None):
            passed, label, remediation = doctor_mod._check_adapter_client_on_path("opencode")

        self.assertFalse(passed)
        self.assertIn("opencode", label.lower())
        self.assertIn("Install", remediation)

    def test_check_adapter_client_on_path_passes_when_client_found(self) -> None:
        """When the adapter client is on PATH, the check must pass."""
        with mock.patch("shutil.which", return_value="/usr/bin/opencode"):
            passed, label, remediation = doctor_mod._check_adapter_client_on_path("opencode")

        self.assertTrue(passed, f"unexpected failure: {remediation}")

    # -- 4.2: doctor with different plan states ----------------------------

    # -- 4.3: run-start preflight is warning-only -----------------------

    def test_run_preflight_warnings_never_blocks_dispatch(self) -> None:
        """Run-start preflight warnings must not raise, exit, or block."""
        try:
            self.opsx_plan.run_preflight_warnings(self.repo, None)
        except Exception as exc:
            self.fail(f"run_preflight_warnings raised unexpectedly: {exc}")

    def test_run_preflight_warnings_logs_stale_install_and_dirty_tree(self) -> None:
        """Warnings should be logged for stale install and dirty tree, but
        the function must always return None."""
        (self.repo / "tracked.txt").write_text("dirty\n", encoding="utf-8")
        logs: list[str] = []

        def capture_log(msg: str) -> None:
            logs.append(msg)

        with mock.patch.object(self.opsx_plan.base, "log", side_effect=capture_log):
            result = self.opsx_plan.run_preflight_warnings(self.repo, None)
        self.assertIsNone(result)
        # At least one warning about dirty tree should appear
        warning_msgs = [m for m in logs if "Tracked tree is clean" in m or "uncommitted" in m.lower()]
        self.assertTrue(warning_msgs, f"No dirty-tree warning in logs: {logs}")

    def test_cmd_run_runs_preflight_warnings_without_changing_outcome(self) -> None:
        """When cmd_run executes, preflight warnings fire but the run continues
        normally."""
        plan_path = self._write_plan_toml(name="preflight-run-plan")
        plan_src = str(plan_path.relative_to(self.repo))

        # Verify plan loads and run_preflight_warnings is called
        preflight_called: list[bool] = []

        def fake_preflight(repo, plan_src_, adapter, cfg=None):
            preflight_called.append(True)

        with mock.patch.object(
            self.opsx_plan, "run_preflight_warnings",
            side_effect=fake_preflight,
        ):
            args = argparse.Namespace(
                repo=str(self.repo),
                plan=plan_src,
                dry_run=True,
                only=None,
                max_changes=0,
                budget_minutes=0,
                budget_usd=0,
                create_only=False,
            )
            rc = self.opsx_plan.cmd_run(args)
        self.assertTrue(preflight_called, "run_preflight_warnings was not called")

    def test_cmd_run_blocks_on_uninitialized_openspec(self) -> None:
        """An uninitialized OpenSpec repo must fail the run before dispatch."""
        plan_path = self._write_plan_toml(name="openspec-gate-plan")
        plan_src = str(plan_path.relative_to(self.repo))

        # No openspec/config.yaml exists in the temp repo.
        with mock.patch.object(
            self.opsx_plan.doctor, "_check_openspec_initialized",
            return_value=(False, "OpenSpec initialized in repo", "openspec init hint"),
        ):
            args = argparse.Namespace(
                repo=str(self.repo),
                plan=plan_src,
                dry_run=False,
                only=None,
                max_changes=0,
                budget_minutes=0,
                budget_usd=0,
                create_only=False,
                no_branch=False,
                no_pr=False,
                skip_warning=False,
                skip_suggestion=False,
                skip_openspec=False,
            )
            stderr = io.StringIO()
            with mock.patch("sys.stderr", stderr):
                rc = self.opsx_plan.cmd_run(args)
        self.assertEqual(rc, 2)
        self.assertIn("openspec init", stderr.getvalue())

    def test_cmd_run_skips_gate_with_skip_openspec(self) -> None:
        """--skip-openspec bypasses the fail-closed gate."""
        plan_path = self._write_plan_toml(name="openspec-skip-plan")
        plan_src = str(plan_path.relative_to(self.repo))

        gate_called: list[bool] = []

        def fake_gate(repo):
            gate_called.append(True)
            return (False, "OpenSpec initialized in repo", "openspec init hint")

        def fake_preflight(repo, plan_src_, adapter, cfg=None):
            return None

        with mock.patch.object(
            self.opsx_plan.doctor, "_check_openspec_initialized", side_effect=fake_gate
        ), mock.patch.object(
            self.opsx_plan, "run_preflight_warnings", side_effect=fake_preflight
        ):
            args = argparse.Namespace(
                repo=str(self.repo),
                plan=plan_src,
                dry_run=False,
                only=None,
                max_changes=0,
                budget_minutes=0,
                budget_usd=0,
                create_only=False,
                no_branch=False,
                no_pr=False,
                skip_warning=False,
                skip_suggestion=False,
                skip_openspec=True,
            )
            rc = self.opsx_plan.cmd_run(args)
        self.assertFalse(gate_called, "gate should be skipped with --skip-openspec")
        self.assertIsNotNone(rc)


class DirectWorkerAgentDoctorCheckTests(unittest.TestCase):
    """Tests for the doctor check that verifies worker agents are installed
    for a direct-dispatch plan (task 7)."""

    def setUp(self) -> None:
        self.opsx_plan = load_opsx_plan()
        self.tmp = tempfile.TemporaryDirectory()
        self.fake_home = Path(self.tmp.name) / "home"
        self.fake_home.mkdir()

    def tearDown(self) -> None:
        self.tmp.cleanup()

    def _direct_cfg(self, adapter: str = "claude-code") -> dict:
        return {
            "adapter": adapter,
            "implement_invoke": "claude -p --agent opsx-implementer",
            "review_invoke": "claude -p --agent opsx-reviewer",
            "archive_invoke": "claude -p --agent opsx-archiver",
        }

    def _write_agents(self, agent_dir: Path, names: tuple[str, ...]) -> None:
        agent_dir.mkdir(parents=True, exist_ok=True)
        for name in names:
            (agent_dir / f"{name}.md").write_text(
                f"---\nname: {name}\n---\n", encoding="utf-8"
            )

    def test_reports_missing_agents_by_name_with_installer(self) -> None:
        agent_dir = self.fake_home / ".claude" / "agents"
        self._write_agents(agent_dir, ("opsx-implementer",))  # reviewer + archiver missing

        with mock.patch.dict(os.environ, {"HOME": str(self.fake_home)}, clear=False):
            passed, label, remediation = doctor_mod._check_direct_worker_agents(
                self._direct_cfg()
            )

        self.assertFalse(passed)
        self.assertIn("opsx-reviewer", remediation)
        self.assertIn("opsx-archiver", remediation)
        self.assertNotIn("opsx-implementer", remediation)
        self.assertIn("adapters/claude-code/install.sh", remediation)

    def test_passes_when_all_worker_agents_present(self) -> None:
        agent_dir = self.fake_home / ".claude" / "agents"
        self._write_agents(
            agent_dir, ("opsx-implementer", "opsx-reviewer", "opsx-archiver")
        )

        with mock.patch.dict(os.environ, {"HOME": str(self.fake_home)}, clear=False):
            passed, label, remediation = doctor_mod._check_direct_worker_agents(
                self._direct_cfg()
            )

        self.assertTrue(passed, remediation)

    def test_skipped_when_no_plan_resolved(self) -> None:
        passed, label, remediation = doctor_mod._check_direct_worker_agents(None)
        self.assertTrue(passed)
        self.assertEqual(remediation, "")

    def test_skipped_when_plan_does_not_use_direct_dispatch(self) -> None:
        cfg = self._direct_cfg()
        cfg["archive_invoke"] = ""  # fewer than three invokes -> nested-controller path

        with mock.patch.dict(os.environ, {"HOME": str(self.fake_home)}, clear=False):
            passed, label, remediation = doctor_mod._check_direct_worker_agents(cfg)

        self.assertTrue(passed)

    def test_opencode_direct_plan_checks_opencode_agent_directory(self) -> None:
        agent_dir = self.fake_home / ".config" / "opencode" / "agents"
        self._write_agents(
            agent_dir, ("opsx-implementer", "opsx-reviewer", "opsx-archiver")
        )
        cfg = self._direct_cfg(adapter="opencode")

        with mock.patch.dict(os.environ, {"HOME": str(self.fake_home)}, clear=False):
            passed, label, remediation = doctor_mod._check_direct_worker_agents(cfg)

        self.assertTrue(passed, remediation)

    def test_passes_when_agents_only_in_repo_local_install(self) -> None:
        repo = Path(self.tmp.name) / "repo"
        agent_dir = repo / ".claude" / "agents"
        self._write_agents(
            agent_dir, ("opsx-implementer", "opsx-reviewer", "opsx-archiver")
        )

        with mock.patch.dict(os.environ, {"HOME": str(self.fake_home)}, clear=False):
            passed, label, remediation = doctor_mod._check_direct_worker_agents(
                self._direct_cfg(), repo
            )

        self.assertTrue(passed, remediation)

    def test_passes_when_agents_only_in_home_install_with_repo_given(self) -> None:
        repo = Path(self.tmp.name) / "repo"
        repo.mkdir()
        agent_dir = self.fake_home / ".claude" / "agents"
        self._write_agents(
            agent_dir, ("opsx-implementer", "opsx-reviewer", "opsx-archiver")
        )

        with mock.patch.dict(os.environ, {"HOME": str(self.fake_home)}, clear=False):
            passed, label, remediation = doctor_mod._check_direct_worker_agents(
                self._direct_cfg(), repo
            )

        self.assertTrue(passed, remediation)

    def test_fails_naming_missing_agents_when_in_neither_location(self) -> None:
        repo = Path(self.tmp.name) / "repo"
        repo.mkdir()

        with mock.patch.dict(os.environ, {"HOME": str(self.fake_home)}, clear=False):
            passed, label, remediation = doctor_mod._check_direct_worker_agents(
                self._direct_cfg(), repo
            )

        self.assertFalse(passed)
        self.assertIn("opsx-implementer", remediation)
        self.assertIn("opsx-reviewer", remediation)
        self.assertIn("opsx-archiver", remediation)
        self.assertIn("adapters/claude-code/install.sh", remediation)


class DshAdapterDoctorTests(unittest.TestCase):
    """dsh adapter registry: the doctor installer mapping and the
    dsh-or-npx executable check (the pinned npx fallback means dsh itself
    need not be installed)."""

    def test_dsh_installer_mapping_registered(self) -> None:
        self.assertEqual(
            doctor_mod._ADAPTER_INSTALLERS["dsh"],
            "adapters/dsh/install.sh",
        )

    def test_dsh_executable_check_passes_with_dsh_on_path(self) -> None:
        with mock.patch("shutil.which", side_effect=lambda name: f"/usr/bin/{name}"):
            passed, label, remediation = doctor_mod._check_adapter_client_on_path("dsh")
        self.assertTrue(passed, f"expected pass: {remediation}")
        self.assertIn("dsh", label)

    def test_dsh_executable_check_passes_with_only_npx_on_path(self) -> None:
        def fake_which(name):
            if name == "dsh":
                return None
            return f"/usr/bin/{name}"

        with mock.patch("shutil.which", side_effect=fake_which):
            passed, label, remediation = doctor_mod._check_adapter_client_on_path("dsh")
        self.assertTrue(passed, f"npx on PATH must satisfy the dsh check: {remediation}")

    def test_dsh_executable_check_fails_when_neither_dsh_nor_npx(self) -> None:
        with mock.patch("shutil.which", return_value=None):
            passed, label, remediation = doctor_mod._check_adapter_client_on_path("dsh")
        self.assertFalse(passed)
        self.assertIn("dsh", label)
        self.assertIn("npx", remediation)


class DoctorProbeCoverageTests(unittest.TestCase):
    """Assert that every _check_* probe defined in doctor.py is invoked by
    both ``run_doctor_checks`` and ``run_preflight_warnings`` in the
    entrypoint (tasks.md 7.6 / design.md D6)."""

    def test_every_doctor_probe_is_called_by_both_aggregators(self) -> None:
        opsx_plan = load_opsx_plan()

        probes = {
            name for name in dir(doctor_mod)
            if name.startswith("_check_")
            and callable(getattr(doctor_mod, name))
            and name != "_check_stale_install"  # lives in the entrypoint
        }

        def _called_doctor_checks(func):
            called = set()
            tree = ast.parse(inspect.getsource(func))
            for node in ast.walk(tree):
                if not isinstance(node, ast.Call):
                    continue
                if not isinstance(node.func, ast.Attribute):
                    continue
                attr = node.func
                if (
                    isinstance(attr.value, ast.Name)
                    and attr.value.id == "doctor"
                    and attr.attr.startswith("_check_")
                ):
                    called.add(attr.attr)
            return called

        doctor_called = _called_doctor_checks(opsx_plan.run_doctor_checks)
        preflight_called = _called_doctor_checks(opsx_plan.run_preflight_warnings)

        missing_doctor = probes - doctor_called
        missing_preflight = probes - preflight_called
        if missing_doctor or missing_preflight:
            self.fail(
                f"doctor.py probes missing from aggregators:\n"
                f"  run_doctor_checks: {sorted(missing_doctor)}\n"
                f"  run_preflight_warnings: {sorted(missing_preflight)}"
            )
