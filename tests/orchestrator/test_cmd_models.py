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
        # cmd_models_init uses cmd_models' local USER_CONFIG_PATH (imported
        # via ``from lib.models.resolver import ...`` at module load time),
        # so patching only resolver.USER_CONFIG_PATH leaves the module-level
        # name pointing at the real user-global file.  Patch the module
        # attribute so the generated models.toml lands in the temp tree.
        self._module_config_patch = mock.patch.object(
            self.opsx_plan.cmd_models, "USER_CONFIG_PATH",
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
            rc = self.opsx_plan.cmd_models.cmd_models_show(args)
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
            rc = self.opsx_plan.cmd_models.cmd_models_env(args)
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
            rc = self.opsx_plan.cmd_models.cmd_models_env(args)
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
                rc = self.opsx_plan.cmd_models.cmd_models_init(args)
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
