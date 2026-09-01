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

class UseCommandTests(unittest.TestCase):
    """opsx-plan use command and active-plan pointer resolution."""

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


    def test_cmd_use_writes_pointer(self) -> None:
        plan = self._write_plan_toml("my-plan.toml")
        args = argparse.Namespace(repo=str(self.repo), plan="my-plan.toml")
        rc = self.opsx_plan.cmd_use.cmd_use(args)
        self.assertEqual(rc, 0)
        pointer = self.opsx_plan.planref.read_active_plan(self.repo)
        self.assertEqual(pointer, "my-plan.toml")


    def test_cmd_use_rejects_nonexistent_plan(self) -> None:
        args = argparse.Namespace(repo=str(self.repo), plan="missing.toml")
        rc = self.opsx_plan.cmd_use.cmd_use(args)
        self.assertEqual(rc, 2)
        self.assertIsNone(self.opsx_plan.planref.read_active_plan(self.repo))


    def test_cmd_use_rejects_invalid_toml(self) -> None:
        p = self.repo / "bad.toml"
        p.write_text("not valid toml {{{", encoding="utf-8")
        args = argparse.Namespace(repo=str(self.repo), plan="bad.toml")
        rc = self.opsx_plan.cmd_use.cmd_use(args)
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
            rc = self.opsx_plan.cmd_use.cmd_use(args)
            self.assertEqual(rc, 2)
            self.assertIsNone(self.opsx_plan.planref.read_active_plan(self.repo))
        finally:
            outside.unlink(missing_ok=True)


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
