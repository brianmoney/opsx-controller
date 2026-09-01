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

class StatusCommandTests(unittest.TestCase):
    """opsx-plan status display: active/inspected header and operator guidance."""

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
                rc = self.opsx_plan.cmd_status.cmd_status(args)
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
                rc = self.opsx_plan.cmd_status.cmd_status(args)
                output = stdout.getvalue()
                self.assertIn("(active: active.toml)", output)
                self.assertIn("[inspected:", output)
        finally:
            self.opsx_plan.run_direct_change = original


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
                rc = self.opsx_plan.cmd_status.cmd_status(args)
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
                rc = self.opsx_plan.cmd_status.cmd_status(args)
                output = stdout.getvalue()
                self.assertIn("(active: same.toml)", output)
                self.assertNotIn("[inspected:", output)
        finally:
            self.opsx_plan.run_direct_change = original

    # ── 5.6: command-level --repo plan-resolution coverage ─────────────


    def test_status_shows_approve_guidance_for_awaiting_approval(self) -> None:
        plan = self._plan_with_gated_changes()
        self._activate_plan(str(plan.relative_to(self.repo)))
        cfg = self.opsx_plan.planref.load_plan(plan)
        state = self.opsx_plan.state_mod.load_state(self.repo, "test-plan")

        import io as _io
        buf = _io.StringIO()
        with mock.patch("sys.stdout", buf):
            self.opsx_plan.cmd_status.cmd_status_inner(cfg, state, header="test", plan_arg=None)
        output = buf.getvalue()
        self.assertIn("\u2192 opsx-plan approve gated-a", output)
        self.assertIn("\u2192 opsx-plan approve gated-b", output)
        self.assertNotIn("\u2192 opsx-plan approve no-gate", output)


    def test_status_lists_manual_follow_up_for_done_change(self) -> None:
        plan = self._plan_with_gated_changes()
        self._activate_plan(str(plan.relative_to(self.repo)))
        cfg = self.opsx_plan.planref.load_plan(plan)
        state = self.opsx_plan.state_mod.load_state(self.repo, "test-plan")
        record = self.opsx_plan.state_mod.rec(state, "gated-a")
        record["status"] = self.opsx_plan.base.DONE
        record["phase"] = "done"
        record["manual_tasks_pending"] = ["4.2 Plant fixtures (manual)"]
        self.opsx_plan.state_mod.save_state(self.repo, "test-plan", state)

        import io as _io
        buf = _io.StringIO()
        with mock.patch("sys.stdout", buf):
            self.opsx_plan.cmd_status.cmd_status_inner(cfg, state, header="test", plan_arg=None)
        output = buf.getvalue()
        self.assertIn("manual follow-up (operator checklist):", output)
        self.assertIn("- 4.2 Plant fixtures (manual)", output)


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
            self.opsx_plan.cmd_status.cmd_status_inner(cfg, state, header="test", plan_arg=None)
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
            self.opsx_plan.cmd_status.cmd_status_inner(cfg, state, header="test", plan_arg=plan_arg)
        output = buf.getvalue()
        self.assertIn(f"\u2192 opsx-plan approve {plan_arg} gated-a", output)


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
            self.opsx_plan.cmd_status.cmd_status_inner(cfg, state, header="test", plan_arg=None)
        output = buf.getvalue()
        self.assertIn("\u2192 opsx-plan accept created-a", output)
        self.assertIn("\u2192 opsx-plan accept created-b", output)
