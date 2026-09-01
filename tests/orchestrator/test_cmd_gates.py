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
            rc = self.opsx_plan.cmd_gates.cmd_approve(args)
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
        self.opsx_plan.cmd_gates.cmd_approve(args)
        # Second call — no changes left awaiting approval
        stdout = io.StringIO()
        with mock.patch("sys.stdout", stdout):
            rc = self.opsx_plan.cmd_gates.cmd_approve(args)
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
            rc = self.opsx_plan.cmd_gates.cmd_approve(args)
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
            rc = self.opsx_plan.cmd_gates.cmd_accept(args)
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
            rc = self.opsx_plan.cmd_gates.cmd_accept(args)
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
            rc = self.opsx_plan.cmd_gates.cmd_accept(args)
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
            rc = self.opsx_plan.cmd_gates.cmd_reset(args)
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
            rc = self.opsx_plan.cmd_gates.cmd_reset(args)
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
            rc = self.opsx_plan.cmd_gates.cmd_reset(args)
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

    def test_single_change_approve_still_works(self) -> None:
        plan = self._plan_with_gated_changes()
        self._activate_plan(str(plan.relative_to(self.repo)))

        args = argparse.Namespace(
            repo=str(self.repo),
            plan=None,
            change=["gated-a"],
            approve_all=False,
        )
        rc = self.opsx_plan.cmd_gates.cmd_approve(args)
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
        rc = self.opsx_plan.cmd_gates.cmd_reset(args)
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
            rc = self.opsx_plan.cmd_gates.cmd_approve(args)
        self.assertEqual(rc, 2)
        self.assertIn("at least one change id is required", stderr.getvalue())

    def test_cmd_approve_resolves_via_active_pointer(self) -> None:
        """cmd_approve with plan=None resolves the plan via the active pointer."""
        self._write_plan_toml(
            "my-plan.toml",
            '[plan]\nname = "test-plan"\nadapter = "opencode"\n\n'
            '[[changes]]\nid = "test-change"\n',
        )
        self.opsx_plan.write_active_plan(self.repo, "my-plan.toml")

        args = argparse.Namespace(
            repo=str(self.repo), plan=None, change=["test-change"],
            approve_all=False,
        )
        rc = self.opsx_plan.cmd_gates.cmd_approve(args)

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
        rc = self.opsx_plan.cmd_gates.cmd_accept(args)

        self.assertEqual(rc, 0)
        state = self.opsx_plan.state_mod.load_state(self.repo, "test-plan")
        self.assertTrue(state["changes"]["test-change"]["accepted"])

    def test_cmd_accept_phase_persists_successes_before_invalid_change(self) -> None:
        """Phase-wide accept must persist valid accepts before one failure."""
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
            rc = self.opsx_plan.cmd_gates.cmd_accept(args)

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
        self._write_plan_toml(
            "my-plan.toml",
            '[plan]\nname = "test-plan"\nadapter = "opencode"\n\n'
            '[[changes]]\nid = "test-change"\n',
        )
        self.opsx_plan.write_active_plan(self.repo, "my-plan.toml")

        args = argparse.Namespace(
            repo=str(self.repo), plan=None, change=["test-change"],
            failed=False,
        )
        rc = self.opsx_plan.cmd_gates.cmd_reset(args)

        self.assertEqual(rc, 0)
        state = self.opsx_plan.state_mod.load_state(self.repo, "test-plan")
        self.assertEqual(state["changes"]["test-change"]["status"], "pending")
