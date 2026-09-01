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
        rc = self.opsx_plan.cmd_archive_plan.cmd_archive_plan(args)
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
        rc = self.opsx_plan.cmd_archive_plan.cmd_archive_plan(args)
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
            rc = self.opsx_plan.cmd_archive_plan.cmd_archive_plan(args)
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
            rc = self.opsx_plan.cmd_archive_plan.cmd_archive_plan(args)
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
            rc = self.opsx_plan.cmd_archive_plan.cmd_archive_plan(args)
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
        rc = self.opsx_plan.cmd_archive_plan.cmd_archive_plan(args)
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
        rc = self.opsx_plan.cmd_archive_plan.cmd_archive_plan(args)
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
