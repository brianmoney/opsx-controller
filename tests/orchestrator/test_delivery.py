from __future__ import annotations

import argparse
import importlib.util
import sys
import os
import subprocess
import tempfile
import textwrap
import unittest
from pathlib import Path
from unittest import mock

from lib.models import resolver
from lib.orchestrator import delivery as delivery_mod
from lib.orchestrator import groundtruth as groundtruth_mod
from lib.orchestrator import state as state_mod
from lib.orchestrator import base as base_mod

SCRIPT = Path(__file__).resolve().parents[2] / "orchestrator" / "opsx-plan.py"

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


class GitDeliveryBranchNameResolutionTests(unittest.TestCase):
    def setUp(self) -> None:
        self.opsx_plan = load_opsx_plan()

    def test_configured_branch_is_used(self) -> None:
        git_delivery_cfg = {"branch": "opsx/my-branch"}
        name = delivery_mod.resolve_delivery_branch_name("my-plan", git_delivery_cfg)
        self.assertEqual(name, "opsx/my-branch")

    def test_derives_from_plan_name_when_unconfigured(self) -> None:
        git_delivery_cfg = {"branch": ""}
        name = delivery_mod.resolve_delivery_branch_name("operator-workflow-upgrades", git_delivery_cfg)
        self.assertEqual(name, "opsx/operator-workflow-upgrades")

    def test_whitespace_only_treated_as_unconfigured(self) -> None:
        git_delivery_cfg = {"branch": "   "}
        name = delivery_mod.resolve_delivery_branch_name("my-plan", git_delivery_cfg)
        self.assertEqual(name, "opsx/my-plan")

class GitDeliveryBaseRefResolutionTests(unittest.TestCase):
    def setUp(self) -> None:
        self.opsx_plan = load_opsx_plan()
        self.tmp = tempfile.TemporaryDirectory()
        self.repo = Path(self.tmp.name)
        subprocess.run(["git", "init"], cwd=self.repo, check=True, capture_output=True, text=True)
        subprocess.run(
            ["git", "-c", "user.email=t@t", "-c", "user.name=T",
             "commit", "--allow-empty", "-m", "init"],
            cwd=self.repo, check=True, capture_output=True, text=True,
        )
        self.default_branch = delivery_mod._git_current_branch(self.repo)

    def tearDown(self) -> None:
        self.tmp.cleanup()

    def test_configured_base_ref_is_used(self) -> None:
        git_delivery_cfg = {"base_ref": "release/next"}
        base_ref, err = delivery_mod.resolve_delivery_base_ref(self.repo, git_delivery_cfg)
        self.assertIsNone(err)
        self.assertEqual(base_ref, "release/next")

    def test_defaults_to_current_branch(self) -> None:
        git_delivery_cfg = {"base_ref": ""}
        base_ref, err = delivery_mod.resolve_delivery_base_ref(self.repo, git_delivery_cfg)
        self.assertIsNone(err)
        self.assertEqual(base_ref, self.default_branch)

class GitDeliveryEnsureBranchTests(unittest.TestCase):
    def setUp(self) -> None:
        self.opsx_plan = load_opsx_plan()
        self.tmp = tempfile.TemporaryDirectory()
        self.repo = Path(self.tmp.name)
        subprocess.run(["git", "init"], cwd=self.repo, check=True, capture_output=True, text=True)
        subprocess.run(
            ["git", "-c", "user.email=t@t", "-c", "user.name=T",
             "commit", "--allow-empty", "-m", "init"],
            cwd=self.repo, check=True, capture_output=True, text=True,
        )
        self.default_branch = delivery_mod._git_current_branch(self.repo)
        self.cfg = {
            "name": "test-plan",
            "git_delivery": {
                "enabled": True,
                "branch": "",
                "base_ref": "",
                "create_pull_request": False,
            },
        }
        self.state = {"plan": "test-plan", "approvals": [], "changes": {},
                       "git_delivery": state_mod._default_git_delivery_state()}

    def tearDown(self) -> None:
        self.tmp.cleanup()

    # 3.1
    def test_first_run_creates_configured_delivery_branch(self) -> None:
        self.cfg["git_delivery"]["branch"] = "opsx/custom-delivery"
        self.cfg["git_delivery"]["base_ref"] = self.default_branch

        proceed, err = delivery_mod.ensure_delivery_branch(
            self.repo, self.cfg, self.state,
        )
        self.assertTrue(proceed, f"expected proceed, got error: {err}")
        self.assertIsNone(err)
        gd = self.state["git_delivery"]
        self.assertEqual(gd["base_ref"], self.default_branch)
        self.assertEqual(gd["branch_name"], "opsx/custom-delivery")
        self.assertEqual(gd["delivery_status"], "branch_ready")
        # Verify branch was created
        self.assertTrue(delivery_mod._git_branch_exists(self.repo, "opsx/custom-delivery"))
        self.assertTrue(delivery_mod._git_current_head_on_branch(self.repo, "opsx/custom-delivery"))

    # 3.2
    def test_first_run_derives_branch_and_default_base_ref(self) -> None:
        proceed, err = delivery_mod.ensure_delivery_branch(
            self.repo, self.cfg, self.state,
        )
        self.assertTrue(proceed, f"expected proceed, got error: {err}")
        gd = self.state["git_delivery"]
        self.assertEqual(gd["base_ref"], self.default_branch)
        self.assertEqual(gd["branch_name"], "opsx/test-plan")
        self.assertTrue(delivery_mod._git_branch_exists(self.repo, "opsx/test-plan"))

    # 3.3
    def test_resume_on_recorded_branch_proceeds(self) -> None:
        # First create the branch and record state
        self.state["git_delivery"]["branch_name"] = "opsx/test-plan"
        self.state["git_delivery"]["base_ref"] = self.default_branch
        self.state["git_delivery"]["delivery_status"] = "branch_ready"
        subprocess.run(
            ["git", "checkout", "-b", "opsx/test-plan"],
            cwd=self.repo, check=True, capture_output=True, text=True,
        )

        proceed, err = delivery_mod.ensure_delivery_branch(
            self.repo, self.cfg, self.state,
        )
        self.assertTrue(proceed, f"expected proceed, got error: {err}")
        self.assertIsNone(err)
        # State should be unchanged
        self.assertEqual(self.state["git_delivery"]["branch_name"], "opsx/test-plan")

    def test_refuse_on_wrong_branch(self) -> None:
        self.state["git_delivery"]["branch_name"] = "opsx/test-plan"
        self.state["git_delivery"]["base_ref"] = self.default_branch
        self.state["git_delivery"]["delivery_status"] = "branch_ready"

        proceed, err = delivery_mod.ensure_delivery_branch(
            self.repo, self.cfg, self.state,
        )
        self.assertFalse(proceed)
        self.assertIn("expected delivery branch", err)
        self.assertIn(self.default_branch, err)

    # 3.4
    def test_dirty_tracked_tree_blocks_branch_creation(self) -> None:
        # Add and commit a tracked file, then dirty it
        (self.repo / "tracked.txt").write_text("clean\n", encoding="utf-8")
        subprocess.run(
            ["git", "add", "tracked.txt"], cwd=self.repo, check=True, capture_output=True, text=True,
        )
        subprocess.run(
            ["git", "-c", "user.email=t@t", "-c", "user.name=T",
             "commit", "-m", "add tracked"],
            cwd=self.repo, check=True, capture_output=True, text=True,
        )
        (self.repo / "tracked.txt").write_text("dirty\n", encoding="utf-8")

        proceed, err = delivery_mod.ensure_delivery_branch(
            self.repo, self.cfg, self.state,
        )
        self.assertFalse(proceed)
        self.assertIn("dirty", err)
        self.assertIsNone(self.state["git_delivery"]["branch_name"])

    # 3.5
    def test_no_branch_skips_first_run_creation(self) -> None:
        proceed, err = delivery_mod.ensure_delivery_branch(
            self.repo, self.cfg, self.state, no_branch=True,
        )
        self.assertTrue(proceed, f"expected proceed, got error: {err}")
        self.assertIsNone(err)
        self.assertIsNone(self.state["git_delivery"]["branch_name"])
        self.assertEqual(self.state["git_delivery"]["delivery_status"], "disabled")

    def test_no_branch_rejected_when_branch_already_recorded(self) -> None:
        self.state["git_delivery"]["branch_name"] = "opsx/test-plan"
        self.state["git_delivery"]["base_ref"] = self.default_branch
        self.state["git_delivery"]["delivery_status"] = "branch_ready"
        subprocess.run(
            ["git", "checkout", "-b", "opsx/test-plan"],
            cwd=self.repo, check=True, capture_output=True, text=True,
        )

        proceed, err = delivery_mod.ensure_delivery_branch(
            self.repo, self.cfg, self.state, no_branch=True,
        )
        self.assertFalse(proceed)
        self.assertIn("cannot use --no-branch", err)
        self.assertIn("already been recorded", err)

    # 3.6
    def test_plan_without_git_delivery_proceeds_normally(self) -> None:
        self.cfg["git_delivery"] = {"enabled": False, "branch": "", "base_ref": "", "create_pull_request": False}
        proceed, err = delivery_mod.ensure_delivery_branch(
            self.repo, self.cfg, self.state,
        )
        self.assertTrue(proceed)
        self.assertIsNone(err)
        self.assertEqual(self.state["git_delivery"]["delivery_status"], "disabled")

    def test_disabled_no_git_operations_performed(self) -> None:
        self.cfg["git_delivery"]["enabled"] = False
        branches_before = subprocess.run(
            ["git", "branch"], cwd=self.repo, check=True,
            capture_output=True, text=True,
        ).stdout
        proceed, err = delivery_mod.ensure_delivery_branch(
            self.repo, self.cfg, self.state,
        )
        self.assertTrue(proceed)
        branches_after = subprocess.run(
            ["git", "branch"], cwd=self.repo, check=True,
            capture_output=True, text=True,
        ).stdout
        self.assertEqual(branches_before, branches_after)

    def test_existing_branch_with_unrecorded_state_transitions(self) -> None:
        """When branch exists on disk but state doesn't record it, the branch
        is adopted and state is updated (handles interrupted first runs)."""
        subprocess.run(
            ["git", "checkout", "-b", "opsx/test-plan"],
            cwd=self.repo, check=True, capture_output=True, text=True,
        )
        # Switch back to default so the "first run" doesn't see branch as checked out
        subprocess.run(
            ["git", "checkout", self.default_branch],
            cwd=self.repo, check=True, capture_output=True, text=True,
        )

        proceed, err = delivery_mod.ensure_delivery_branch(
            self.repo, self.cfg, self.state,
        )
        self.assertTrue(proceed, f"expected proceed, got error: {err}")
        self.assertIsNone(err)
        gd = self.state["git_delivery"]
        self.assertEqual(gd["branch_name"], "opsx/test-plan")
        self.assertEqual(gd["base_ref"], self.default_branch)
        self.assertEqual(gd["delivery_status"], "branch_ready")
        # Branch should now be checked out
        self.assertTrue(delivery_mod._git_current_head_on_branch(self.repo, "opsx/test-plan"))

class GitDeliveryCmdRunIntegrationTests(unittest.TestCase):
    def setUp(self) -> None:
        self.opsx_plan = load_opsx_plan()
        self.tmp = tempfile.TemporaryDirectory()
        self.repo = Path(self.tmp.name)
        subprocess.run(["git", "init"], cwd=self.repo, check=True, capture_output=True, text=True)
        subprocess.run(
            ["git", "-c", "user.email=t@t", "-c", "user.name=T",
             "commit", "--allow-empty", "-m", "init"],
            cwd=self.repo, check=True, capture_output=True, text=True,
        )
        # Create a minimal plan TOML
        self.plan_path = self.repo / "test.toml"
        self.plan_path.write_text(textwrap.dedent("""\
            [plan]
            name = "test-gd"
            adapter = "opencode"

            [plan.git_delivery]
            enabled = true

            [[changes]]
            id = "test-change"
        """), encoding="utf-8")
        # Create the authored change directory so the orchestrator can drive it
        cdir = self.repo / "openspec" / "changes" / "test-change"
        cdir.mkdir(parents=True)
        (cdir / "proposal.md").write_text("## Why\n", encoding="utf-8")
        (cdir / "tasks.md").write_text("## 1. Tasks\n\n- [ ] 1.1 Task\n", encoding="utf-8")

    def tearDown(self) -> None:
        self.tmp.cleanup()

    def test_cmd_run_creates_delivery_branch(self) -> None:
        writes: list[str] = []

        def fake_write_active(repo, rel):
            writes.append(rel)

        def fake_run_direct_change(repo, cfg, state, cid, budget_deadline=None, budget_usd=0.0):
            return base_mod.DONE

        def fake_reconcile(repo, cfg, state):
            pass

        def fake_preflight(repo, plan_src, adapter, cfg=None):
            pass

        def fake_cmd_status_inner(cfg, state, header="", plan_arg=None, repo=None):
            return 0

        with mock.patch.object(self.opsx_plan, "write_active_plan", side_effect=fake_write_active), \
             mock.patch.object(self.opsx_plan, "run_direct_change", side_effect=fake_run_direct_change), \
             mock.patch.object(self.opsx_plan, "reconcile", side_effect=fake_reconcile), \
             mock.patch.object(self.opsx_plan, "run_preflight_warnings", side_effect=fake_preflight), \
             mock.patch.object(self.opsx_plan.cmd_status, "cmd_status_inner", side_effect=fake_cmd_status_inner):

            args = argparse.Namespace(
                repo=str(self.repo),
                plan="test.toml",
                dry_run=False,
                skip_openspec=True,
                only=None,
                max_changes=0,
                budget_minutes=0,
                budget_usd=0,
                create_only=False,
                no_branch=False,
            )
            rc = self.opsx_plan.cmd_run(args)
            self.assertEqual(rc, 0)
            # Branch should have been created
            self.assertTrue(delivery_mod._git_branch_exists(self.repo, "opsx/test-gd"))

    def test_cmd_run_with_no_branch_skips_creation(self) -> None:
        def fake_run_direct_change(repo, cfg, state, cid, budget_deadline=None, budget_usd=0.0):
            return base_mod.DONE

        def fake_reconcile(repo, cfg, state):
            pass

        def fake_preflight(repo, plan_src, adapter, cfg=None):
            pass

        def fake_cmd_status_inner(cfg, state, header="", plan_arg=None, repo=None):
            return 0

        with mock.patch.object(self.opsx_plan, "write_active_plan"), \
             mock.patch.object(self.opsx_plan, "run_direct_change", side_effect=fake_run_direct_change), \
             mock.patch.object(self.opsx_plan, "reconcile", side_effect=fake_reconcile), \
             mock.patch.object(self.opsx_plan, "run_preflight_warnings", side_effect=fake_preflight), \
             mock.patch.object(self.opsx_plan.cmd_status, "cmd_status_inner", side_effect=fake_cmd_status_inner):

            args = argparse.Namespace(
                repo=str(self.repo),
                plan="test.toml",
                dry_run=False,
                skip_openspec=True,
                only=None,
                max_changes=0,
                budget_minutes=0,
                budget_usd=0,
                create_only=False,
                no_branch=True,
            )
            rc = self.opsx_plan.cmd_run(args)
            self.assertEqual(rc, 0)
            # Branch should NOT have been created
            self.assertFalse(delivery_mod._git_branch_exists(self.repo, "opsx/test-gd"))

    def test_cmd_run_refuses_with_delivery_error(self) -> None:
        """When ensure_delivery_branch returns an error, cmd_run exits early."""
        # Make the worktree dirty by adding and modifying a tracked file
        (self.repo / "tracked.txt").write_text("clean\n", encoding="utf-8")
        subprocess.run(
            ["git", "add", "tracked.txt"], cwd=self.repo, check=True, capture_output=True, text=True,
        )
        subprocess.run(
            ["git", "-c", "user.email=t@t", "-c", "user.name=T",
             "commit", "-m", "add tracked"],
            cwd=self.repo, check=True, capture_output=True, text=True,
        )
        (self.repo / "tracked.txt").write_text("dirty\n", encoding="utf-8")

        def fake_reconcile(repo, cfg, state):
            pass

        def fake_preflight(repo, plan_src, adapter, cfg=None):
            pass

        with mock.patch.object(self.opsx_plan, "write_active_plan"), \
             mock.patch.object(self.opsx_plan, "reconcile", side_effect=fake_reconcile), \
             mock.patch.object(self.opsx_plan, "run_preflight_warnings", side_effect=fake_preflight):

            args = argparse.Namespace(
                repo=str(self.repo),
                plan="test.toml",
                dry_run=False,
                skip_openspec=True,
                only=None,
                max_changes=0,
                budget_minutes=0,
                budget_usd=0,
                create_only=False,
                no_branch=False,
            )
            rc = self.opsx_plan.cmd_run(args)
            self.assertEqual(rc, 2)

    def test_cmd_run_dry_run_does_not_create_branch(self) -> None:
        """Dry-run must not create or record a delivery branch."""
        def fake_reconcile(repo, cfg, state):
            pass

        def fake_preflight(repo, plan_src, adapter, cfg=None):
            pass

        def fake_cmd_status_inner(cfg, state, header="", plan_arg=None, repo=None):
            return 0

        with mock.patch.object(self.opsx_plan, "write_active_plan"), \
             mock.patch.object(self.opsx_plan, "reconcile", side_effect=fake_reconcile), \
             mock.patch.object(self.opsx_plan, "run_preflight_warnings", side_effect=fake_preflight), \
             mock.patch.object(self.opsx_plan.cmd_status, "cmd_status_inner", side_effect=fake_cmd_status_inner):

            args = argparse.Namespace(
                repo=str(self.repo),
                plan="test.toml",
                dry_run=True,
                skip_openspec=True,
                only=None,
                max_changes=0,
                budget_minutes=0,
                budget_usd=0,
                create_only=False,
                no_branch=False,
            )
            rc = self.opsx_plan.cmd_run(args)
            self.assertEqual(rc, 0)
            # Branch must NOT have been created
            self.assertFalse(delivery_mod._git_branch_exists(self.repo, "opsx/test-gd"))
            # State must NOT have a recorded delivery branch
            state = state_mod.load_state(self.repo, "test-gd")
            gd = state.get("git_delivery", {})
            self.assertIsNone(gd.get("branch_name"))
            self.assertEqual(gd.get("delivery_status"), "disabled")

class GitDeliveryDefaultOffTests(unittest.TestCase):
    """Verify that plans without git_delivery config behave exactly as today."""
    def setUp(self) -> None:
        self.opsx_plan = load_opsx_plan()
        self.tmp = tempfile.TemporaryDirectory()
        self.repo = Path(self.tmp.name)
        subprocess.run(["git", "init"], cwd=self.repo, check=True, capture_output=True, text=True)
        subprocess.run(
            ["git", "-c", "user.email=t@t", "-c", "user.name=T",
             "commit", "--allow-empty", "-m", "init"],
            cwd=self.repo, check=True, capture_output=True, text=True,
        )
        self.cfg = {
            "name": "default-off-plan",
            "git_delivery": self.opsx_plan.planref._parse_git_delivery_config({}),
        }
        self.state = {"plan": "default-off-plan", "approvals": [], "changes": {},
                       "git_delivery": state_mod._default_git_delivery_state()}

    def tearDown(self) -> None:
        self.tmp.cleanup()

    def test_disabled_by_default_proceeds_without_branch_creation(self) -> None:
        proceed, err = delivery_mod.ensure_delivery_branch(
            self.repo, self.cfg, self.state,
        )
        self.assertTrue(proceed)
        self.assertIsNone(err)
        self.assertIsNone(self.state["git_delivery"]["branch_name"])

    def test_build_single_change_config_has_disabled_git_delivery(self) -> None:
        cdir = self.repo / "openspec" / "changes" / "test-change"
        cdir.mkdir(parents=True)
        (cdir / "proposal.md").write_text("## Why\n", encoding="utf-8")
        (cdir / "tasks.md").write_text("## 1. Tasks\n\n- [ ] 1.1 Task\n", encoding="utf-8")
        cfg = self.opsx_plan.build_single_change_config(self.repo, "test-change")
        self.assertIn("git_delivery", cfg)
        self.assertFalse(cfg["git_delivery"]["enabled"])

class PRDeliveryTests(unittest.TestCase):
    """Tests for PR delivery: preflight, creation, idempotency, --no-pr,
    body generation, and fail-closed behaviour."""

    def setUp(self) -> None:
        self.opsx_plan = load_opsx_plan()
        self.tmp = tempfile.TemporaryDirectory()
        self.repo = Path(self.tmp.name)
        git(self.repo, "init")
        git(
            self.repo,
            "remote", "add", "origin",
            "git@github.com:example/repo.git",
        )
        (self.repo / "tracked.txt").write_text("base\n", encoding="utf-8")
        git(self.repo, "add", "tracked.txt")
        git(
            self.repo,
            "-c", "user.email=test@example.invalid",
            "-c", "user.name=Test User",
            "commit", "-m", "init",
        )
        self._saved_which = self.opsx_plan.shutil.which

    def tearDown(self) -> None:
        self.opsx_plan.shutil.which = self._saved_which
        self.tmp.cleanup()

    def _cfg_with_pr_delivery(self, enabled=True, create_pr=True):
        return {
            "name": "test-plan",
            "adapter": "opencode",
            "git_delivery": {
                "enabled": enabled,
                "branch": "",
                "base_ref": "main",
                "create_pull_request": create_pr,
            },
        }

    def _state_with_delivery(self, branch_name="opsx/test-plan",
                              base_ref="main", delivery_status="branch_ready",
                              pull_request_url=None):
        return {
            "plan": "test-plan",
            "approvals": [],
            "changes": {},
            "git_delivery": {
                "base_ref": base_ref,
                "branch_name": branch_name,
                "delivery_status": delivery_status,
                "pull_request_url": pull_request_url,
            },
        }

    # -- Preflight tests ---------------------------------------------------

    def test_pr_preflight_passes_when_gh_and_remote_exist(self) -> None:
        self.opsx_plan.shutil.which = lambda cmd: cmd == "gh"
        cfg = self._cfg_with_pr_delivery()
        ok, err, remote = delivery_mod.check_pr_delivery_prerequisites(self.repo, cfg)
        self.assertTrue(ok, err)
        self.assertIsNone(err)
        self.assertEqual(remote, "origin")

    def test_pr_preflight_fails_when_gh_missing(self) -> None:
        self.opsx_plan.shutil.which = lambda cmd: False
        cfg = self._cfg_with_pr_delivery()
        ok, err, remote = delivery_mod.check_pr_delivery_prerequisites(self.repo, cfg)
        self.assertFalse(ok)
        self.assertIsNotNone(err)
        self.assertIsNone(remote)
        self.assertIn("gh", err.lower())

    def test_pr_preflight_skipped_when_create_pr_false(self) -> None:
        self.opsx_plan.shutil.which = lambda cmd: False
        cfg = self._cfg_with_pr_delivery(create_pr=False)
        ok, err, remote = delivery_mod.check_pr_delivery_prerequisites(self.repo, cfg)
        self.assertTrue(ok, err)
        self.assertIsNone(err)
        self.assertIsNone(remote)

    def test_pr_preflight_fails_when_no_remote(self) -> None:
        self.opsx_plan.shutil.which = lambda cmd: cmd == "gh"
        git(self.repo, "remote", "remove", "origin")
        cfg = self._cfg_with_pr_delivery()
        ok, err, remote = delivery_mod.check_pr_delivery_prerequisites(self.repo, cfg)
        self.assertFalse(ok)
        self.assertIsNotNone(err)
        self.assertIsNone(remote)
        self.assertIn("remote", err.lower())

    def test_pr_preflight_resolves_non_origin_remote(self) -> None:
        """Preflight must pick up a non-origin remote and return its name."""
        self.opsx_plan.shutil.which = lambda cmd: cmd == "gh"
        git(self.repo, "remote", "remove", "origin")
        git(self.repo, "remote", "add", "upstream",
            "git@github.com:example/repo.git")
        cfg = self._cfg_with_pr_delivery()
        ok, err, remote = delivery_mod.check_pr_delivery_prerequisites(self.repo, cfg)
        self.assertTrue(ok, f"preflight should pass with non-origin remote: {err}")
        self.assertIsNone(err)
        self.assertEqual(remote, "upstream")

    def test_pr_preflight_prefers_origin_when_multiple_remotes(self) -> None:
        """When multiple remotes exist, preflight must prefer 'origin'."""
        self.opsx_plan.shutil.which = lambda cmd: cmd == "gh"
        git(self.repo, "remote", "add", "upstream",
            "git@github.com:example/upstream.git")
        cfg = self._cfg_with_pr_delivery()
        ok, err, remote = delivery_mod.check_pr_delivery_prerequisites(self.repo, cfg)
        self.assertTrue(ok, err)
        self.assertIsNone(err)
        self.assertEqual(remote, "origin",
                         "must prefer origin when multiple remotes exist")

    # -- --no-pr tests -----------------------------------------------------

    def test_no_pr_skips_preflight(self) -> None:
        """Even when gh is missing, --no-pr should skip the preflight
        check because it suppresses PR delivery entirely."""
        self.opsx_plan.shutil.which = lambda cmd: False
        # The pgm-level test: cmd_run should not call preflight when no_pr
        cfg = self._cfg_with_pr_delivery()
        state = self._state_with_delivery()
        # Unit test the attempt_pr_delivery function directly
        ok, err = delivery_mod.attempt_pr_delivery(
            self.repo, cfg, state, no_pr=True,
        )
        self.assertTrue(ok)
        self.assertIsNone(err)
        # PR URL should not be set
        self.assertIsNone(state["git_delivery"]["pull_request_url"])

    # -- Push tests --------------------------------------------------------

    def test_push_delivery_branch_succeeds(self) -> None:
        cfg = self._cfg_with_pr_delivery()
        state = self._state_with_delivery(branch_name="opsx/test-plan")

        real_git = groundtruth_mod.git

        def fake_git(repo, *args):
            if args[0] == "push":
                result = mock.Mock()
                result.returncode = 0
                result.stderr = ""
                return result
            return real_git(repo, *args)

        with mock.patch("lib.orchestrator.groundtruth.git", side_effect=fake_git):
            ok, err = delivery_mod.push_delivery_branch(self.repo, cfg, state)
        self.assertTrue(ok, err)
        self.assertIsNone(err)

    def test_push_delivery_branch_fails_closed(self) -> None:
        cfg = self._cfg_with_pr_delivery()
        state = self._state_with_delivery(branch_name="opsx/test-plan")

        real_git = groundtruth_mod.git

        def fake_git(repo, *args):
            if args[0] == "push":
                result = mock.Mock()
                result.returncode = 1
                result.stderr = "push rejected: permission denied"
                return result
            return real_git(repo, *args)

        with mock.patch("lib.orchestrator.groundtruth.git", side_effect=fake_git):
            ok, err = delivery_mod.push_delivery_branch(self.repo, cfg, state)
        self.assertFalse(ok)
        self.assertIsNotNone(err)
        self.assertIn("failed", err.lower())

    def test_push_uses_remote_from_state_instead_of_hardcoding_origin(self) -> None:
        """push_delivery_branch must use the remote_name stored in state,
        not hardcode 'origin'."""
        cfg = self._cfg_with_pr_delivery()
        state = self._state_with_delivery(branch_name="opsx/test-plan")
        state["git_delivery"]["remote_name"] = "upstream"

        push_args: list[tuple] = []

        def fake_git(repo, *args):
            if args[0] == "push":
                push_args.append(tuple(args))
                result = mock.Mock()
                result.returncode = 0
                result.stderr = ""
                return result
            return groundtruth_mod.git(repo, *args)

        with mock.patch("lib.orchestrator.groundtruth.git", side_effect=fake_git):
            ok, err = delivery_mod.push_delivery_branch(self.repo, cfg, state)
        self.assertTrue(ok, err)
        self.assertIsNone(err)
        self.assertGreaterEqual(len(push_args), 1)
        # The remote in the push command must be "upstream", not "origin"
        self.assertIn("upstream", push_args[0])
        self.assertNotIn("origin", push_args[0])

    # -- gh pr create tests ------------------------------------------------

    def test_create_pr_succeeds_and_records_url(self) -> None:
        cfg = self._cfg_with_pr_delivery()
        state = self._state_with_delivery(branch_name="opsx/test-plan")

        real_run = subprocess.run

        def fake_run(cmd, **kwargs):
            if cmd[0] == "gh" and cmd[1] == "pr":
                result = mock.Mock()
                result.returncode = 0
                result.stdout = "https://github.com/example/repo/pull/42"
                result.stderr = ""
                return result
            return real_run(cmd, **kwargs)

        with mock.patch("subprocess.run", side_effect=fake_run):
            ok, err, pr_url = delivery_mod.create_github_pull_request(
                self.repo, cfg, state, "test body",
            )
        self.assertTrue(ok, err)
        self.assertIsNone(err)
        self.assertEqual(pr_url, "https://github.com/example/repo/pull/42")

    def test_create_pr_fails_closed_on_gh_error(self) -> None:
        cfg = self._cfg_with_pr_delivery()
        state = self._state_with_delivery(branch_name="opsx/test-plan")

        real_run = subprocess.run

        def fake_run(cmd, **kwargs):
            if cmd[0] == "gh" and cmd[1] == "pr":
                result = mock.Mock()
                result.returncode = 1
                result.stdout = ""
                result.stderr = "pull request already exists"
                return result
            return real_run(cmd, **kwargs)

        with mock.patch("subprocess.run", side_effect=fake_run):
            ok, err, pr_url = delivery_mod.create_github_pull_request(
                self.repo, cfg, state, "test body",
            )
        self.assertFalse(ok)
        self.assertIsNotNone(err)
        self.assertIsNone(pr_url)
        self.assertIn("failed", err.lower())

    # -- Idempotency tests ------------------------------------------------

    def test_idempotent_rerun_skips_pr_when_already_recorded(self) -> None:
        cfg = self._cfg_with_pr_delivery()
        pr_url = "https://github.com/example/repo/pull/99"
        state = self._state_with_delivery(
            branch_name="opsx/test-plan",
            delivery_status="pr_opened",
            pull_request_url=pr_url,
        )

        # Mock push to ensure it's NOT called (idempotency should skip)
        with mock.patch.object(
            delivery_mod, "push_delivery_branch",
        ) as mock_push:
            ok, err = delivery_mod.attempt_pr_delivery(
                self.repo, cfg, state,
            )
        self.assertTrue(ok)
        self.assertIsNone(err)
        mock_push.assert_not_called()
        # PR URL must remain unchanged
        self.assertEqual(state["git_delivery"]["pull_request_url"], pr_url)

    # -- Full delivery flow tests -----------------------------------------

    def test_full_delivery_flow_records_pr(self) -> None:
        cfg = {
            "name": "test-plan",
            "adapter": "opencode",
            "git_delivery": {
                "enabled": True,
                "branch": "",
                "base_ref": "main",
                "create_pull_request": True,
            },
            "order": [],
            "changes": {},
        }
        state = self._state_with_delivery(branch_name="opsx/test-plan")

        call_count = {"push": 0, "pr": 0}
        real_run = subprocess.run
        real_git = groundtruth_mod.git

        def fake_git(repo, *args):
            if args[0] == "push":
                call_count["push"] += 1
                result = mock.Mock()
                result.returncode = 0
                result.stderr = ""
                return result
            return real_git(repo, *args)

        def fake_run(cmd, **kwargs):
            if cmd[0] == "gh" and cmd[1] == "pr":
                call_count["pr"] += 1
                result = mock.Mock()
                result.returncode = 0
                result.stdout = "https://github.com/example/repo/pull/123"
                result.stderr = ""
                return result
            return real_run(cmd, **kwargs)

        with mock.patch("lib.orchestrator.groundtruth.git", side_effect=fake_git), \
             mock.patch("subprocess.run", side_effect=fake_run):
            ok, err = delivery_mod.attempt_pr_delivery(
                self.repo, cfg, state,
            )

        self.assertTrue(ok)
        self.assertIsNone(err)
        self.assertEqual(call_count["push"], 1)
        self.assertEqual(call_count["pr"], 1)
        self.assertEqual(
            state["git_delivery"]["pull_request_url"],
            "https://github.com/example/repo/pull/123",
        )
        self.assertEqual(state["git_delivery"]["delivery_status"], "pr_opened")

    def test_push_failure_leaves_state_unambiguous(self) -> None:
        cfg = self._cfg_with_pr_delivery()
        state = self._state_with_delivery(branch_name="opsx/test-plan")

        real_git = groundtruth_mod.git

        def fake_git(repo, *args):
            if args[0] == "push":
                result = mock.Mock()
                result.returncode = 1
                result.stderr = "push rejected"
                return result
            return real_git(repo, *args)

        with mock.patch("lib.orchestrator.groundtruth.git", side_effect=fake_git):
            ok, err = delivery_mod.attempt_pr_delivery(
                self.repo, cfg, state,
            )

        self.assertFalse(ok)
        self.assertIsNotNone(err)
        # State must NOT claim PR opened
        self.assertIsNone(state["git_delivery"]["pull_request_url"])
        self.assertNotEqual(state["git_delivery"]["delivery_status"], "pr_opened")

    def test_pr_creation_failure_after_push_leaves_state_unambiguous(self) -> None:
        cfg = {
            "name": "test-plan",
            "adapter": "opencode",
            "git_delivery": {
                "enabled": True,
                "branch": "",
                "base_ref": "main",
                "create_pull_request": True,
            },
            "order": [],
            "changes": {},
        }
        state = self._state_with_delivery(branch_name="opsx/test-plan")

        real_git = groundtruth_mod.git
        real_run = subprocess.run

        def fake_git(repo, *args):
            if args[0] == "push":
                result = mock.Mock()
                result.returncode = 0
                result.stderr = ""
                return result
            return real_git(repo, *args)

        def fake_run(cmd, **kwargs):
            if cmd[0] == "gh" and cmd[1] == "pr":
                result = mock.Mock()
                result.returncode = 1
                result.stdout = ""
                result.stderr = "pull request creation failed"
                return result
            return real_run(cmd, **kwargs)

        with mock.patch("lib.orchestrator.groundtruth.git", side_effect=fake_git), \
             mock.patch("subprocess.run", side_effect=fake_run):
            ok, err = delivery_mod.attempt_pr_delivery(
                self.repo, cfg, state,
            )

        self.assertFalse(ok)
        self.assertIsNotNone(err)
        # State must NOT claim PR opened
        self.assertIsNone(state["git_delivery"]["pull_request_url"])
        self.assertNotEqual(state["git_delivery"]["delivery_status"], "pr_opened")

    # -- Body generation tests --------------------------------------------

    def test_generate_pr_body_includes_change_summary(self) -> None:
        cfg = {
            "name": "test-plan",
            "adapter": "opencode",
            "git_delivery": {"create_pull_request": True},
            "order": [],
            "changes": {},
        }
        state = {"changes": {}}
        body = delivery_mod.generate_pr_body(self.repo, cfg, state)
        self.assertIn("test-plan", body)
        self.assertIn("opsx-plan", body)

    def test_pr_body_fallback_lists_per_change_status_when_no_telemetry(self) -> None:
        """When metrics aggregator is unavailable, the fallback path must
        list per-change status and mention the plan name."""
        cfg = {
            "name": "test-plan",
            "adapter": "opencode",
            "git_delivery": {"create_pull_request": True},
            "order": ["add-example", "fix-bug"],
            "changes": {
                "add-example": {"id": "add-example", "enabled": True},
                "fix-bug": {"id": "fix-bug", "enabled": True},
            },
        }
        state = {
            "changes": {
                "add-example": {"status": "done"},
                "fix-bug": {"status": "running"},
            },
        }
        body = delivery_mod.generate_pr_body(self.repo, cfg, state)
        self.assertIn("test-plan", body)
        self.assertIn("add-example", body)
        self.assertIn("done", body.lower())
        self.assertIn("fix-bug", body)
        self.assertIn("running", body.lower())

    def test_pr_body_fallback_excludes_disabled_changes(self) -> None:
        """Fallback body must not list disabled changes."""
        cfg = {
            "name": "test-plan",
            "adapter": "opencode",
            "git_delivery": {"create_pull_request": True},
            "order": ["enabled-change", "disabled-change"],
            "changes": {
                "enabled-change": {"id": "enabled-change", "enabled": True},
                "disabled-change": {"id": "disabled-change", "enabled": False},
            },
        }
        state = {
            "changes": {
                "enabled-change": {"status": "done"},
                "disabled-change": {"status": "pending"},
            },
        }
        body = delivery_mod.generate_pr_body(self.repo, cfg, state)
        self.assertIn("enabled-change", body)
        self.assertNotIn("disabled-change", body)

    def test_pr_body_with_telemetry_includes_table_and_cost(self) -> None:
        """When metrics aggregator returns change data with cost, the PR
        body must include the summary table and total cost estimate."""
        cfg = {
            "name": "test-plan",
            "adapter": "opencode",
            "git_delivery": {"create_pull_request": True},
            "order": ["add-example"],
            "changes": {
                "add-example": {"id": "add-example", "enabled": True},
            },
        }
        state = {"changes": {}}

        # Build a fake AggregationError for the except clause fallback,
        # and a fake change metric for the imported path.
        FakeChangeMetric = mock.MagicMock()
        FakeChangeMetric.change_id = "add-example"
        FakeChangeMetric.status = "done"
        FakeChangeMetric.total_rounds = 3
        FakeChangeMetric.duration_ms = 125000  # 2m5s
        FakeChangeMetric.tokens = 1_500_000  # 1.5M
        FakeChangeMetric.cost_status = "estimated"
        FakeChangeMetric.estimated_cost = 0.42

        def fake_read_telemetry(repo, plan_name):
            return ([], None)

        def fake_select_run(records, run_id):
            return [], None, None

        def fake_read_state(repo, plan_name):
            return state, None

        def fake_change_aggregation(state_for_cm, selected_records,
                                    plan_name, extra):
            return [FakeChangeMetric], None

        fake_aggregator = mock.MagicMock()
        fake_aggregator.AggregationError = type("AggregationError", (Exception,), {})
        fake_aggregator._change_aggregation = fake_change_aggregation
        fake_aggregator._read_state = fake_read_state
        fake_aggregator._read_telemetry = fake_read_telemetry
        fake_aggregator._select_run = fake_select_run

        with mock.patch.dict(
            "sys.modules",
            {"lib.metrics": mock.MagicMock(), "lib.metrics.aggregator": fake_aggregator},
        ):
            body = delivery_mod.generate_pr_body(self.repo, cfg, state)

        self.assertIn("Change Summary", body)
        self.assertIn("add-example", body)
        self.assertIn("done", body)
        self.assertIn("3", body)  # rounds
        self.assertIn("2m5s", body)  # duration
        self.assertIn("1.5M", body)  # tokens
        self.assertIn("$0.42", body)  # cost
        self.assertIn("Total estimated cost", body)

    def test_pr_body_with_telemetry_handles_unresolved_cost(self) -> None:
        """When cost is unresolved, the body must show 'unresolved' in the
        cost column and note 'partial' on the total cost line."""
        cfg = {
            "name": "test-plan",
            "adapter": "opencode",
            "git_delivery": {"create_pull_request": True},
            "order": ["add-example"],
            "changes": {
                "add-example": {"id": "add-example", "enabled": True},
            },
        }
        state = {"changes": {}}

        FakeChangeMetric = mock.MagicMock()
        FakeChangeMetric.change_id = "add-example"
        FakeChangeMetric.status = "done"
        FakeChangeMetric.total_rounds = 1
        FakeChangeMetric.duration_ms = None
        FakeChangeMetric.tokens = None
        FakeChangeMetric.cost_status = "unresolved"
        FakeChangeMetric.estimated_cost = None

        fake_aggregator = mock.MagicMock()
        fake_aggregator.AggregationError = type("AggregationError", (Exception,), {})
        fake_aggregator._change_aggregation = lambda *a, **kw: ([FakeChangeMetric], None)
        fake_aggregator._read_state = lambda *a, **kw: (state, None)
        fake_aggregator._read_telemetry = lambda *a, **kw: ([], None)
        fake_aggregator._select_run = lambda *a, **kw: ([], None, None)

        with mock.patch.dict(
            "sys.modules",
            {"lib.metrics": mock.MagicMock(), "lib.metrics.aggregator": fake_aggregator},
        ):
            body = delivery_mod.generate_pr_body(self.repo, cfg, state)

        self.assertIn("unresolved", body)
        self.assertNotIn("Total estimated cost", body)

    def test_pr_body_with_telemetry_handles_partial_cost(self) -> None:
        """When cost_status is 'partial' (mixed estimated + unresolved),
        the body must show the estimated cost amount with '(partial)' in the
        per-change column, include it in the total, and mark the total as
        partial."""
        cfg = {
            "name": "test-plan",
            "adapter": "opencode",
            "git_delivery": {"create_pull_request": True},
            "order": ["add-example", "fix-bug"],
            "changes": {
                "add-example": {"id": "add-example", "enabled": True},
                "fix-bug": {"id": "fix-bug", "enabled": True},
            },
        }
        state = {"changes": {}}

        PartialMetric = mock.MagicMock()
        PartialMetric.change_id = "add-example"
        PartialMetric.status = "done"
        PartialMetric.total_rounds = 3
        PartialMetric.duration_ms = 125000
        PartialMetric.tokens = 1_500_000
        PartialMetric.cost_status = "partial"
        PartialMetric.estimated_cost = 0.42

        UnresolvedMetric = mock.MagicMock()
        UnresolvedMetric.change_id = "fix-bug"
        UnresolvedMetric.status = "done"
        UnresolvedMetric.total_rounds = 2
        UnresolvedMetric.duration_ms = None
        UnresolvedMetric.tokens = None
        UnresolvedMetric.cost_status = "unresolved"
        UnresolvedMetric.estimated_cost = None

        cm_list = [PartialMetric, UnresolvedMetric]

        fake_aggregator = mock.MagicMock()
        fake_aggregator.AggregationError = type("AggregationError", (Exception,), {})
        fake_aggregator._change_aggregation = lambda *a, **kw: (cm_list, None)
        fake_aggregator._read_state = lambda *a, **kw: (state, None)
        fake_aggregator._read_telemetry = lambda *a, **kw: ([], None)
        fake_aggregator._select_run = lambda *a, **kw: ([], None, None)

        with mock.patch.dict(
            "sys.modules",
            {"lib.metrics": mock.MagicMock(), "lib.metrics.aggregator": fake_aggregator},
        ):
            body = delivery_mod.generate_pr_body(self.repo, cfg, state)

        # Per-change column: partial change shows dollar amount with "(partial)"
        self.assertIn("$0.42 (partial)", body)
        # Pure unresolved still shows "unresolved"
        self.assertIn("unresolved", body)
        # Total cost line appears with "(partial)" marker
        self.assertIn("Total estimated cost (partial):", body)
        self.assertIn("$0.42", body)

    def test_pr_body_with_telemetry_excludes_disabled_changes(self) -> None:
        """Telemetry-backed PR body must not render disabled changes or include
        their cost in the total."""
        cfg = {
            "name": "test-plan",
            "adapter": "opencode",
            "git_delivery": {"create_pull_request": True},
            "order": ["enabled-change", "disabled-change"],
            "changes": {
                "enabled-change": {"id": "enabled-change", "enabled": True},
                "disabled-change": {"id": "disabled-change", "enabled": False},
            },
        }
        state = {"changes": {}}

        EnabledMetric = mock.MagicMock()
        EnabledMetric.change_id = "enabled-change"
        EnabledMetric.status = "done"
        EnabledMetric.total_rounds = 2
        EnabledMetric.duration_ms = 90000
        EnabledMetric.tokens = 500_000
        EnabledMetric.cost_status = "estimated"
        EnabledMetric.estimated_cost = 0.25

        DisabledMetric = mock.MagicMock()
        DisabledMetric.change_id = "disabled-change"
        DisabledMetric.status = "skipped"
        DisabledMetric.total_rounds = 0
        DisabledMetric.duration_ms = None
        DisabledMetric.tokens = None
        DisabledMetric.cost_status = "unavailable"
        DisabledMetric.estimated_cost = None

        cm_list = [EnabledMetric, DisabledMetric]

        fake_aggregator = mock.MagicMock()
        fake_aggregator.AggregationError = type("AggregationError", (Exception,), {})
        fake_aggregator._change_aggregation = lambda *a, **kw: (cm_list, None)
        fake_aggregator._read_state = lambda *a, **kw: (state, None)
        fake_aggregator._read_telemetry = lambda *a, **kw: ([], None)
        fake_aggregator._select_run = lambda *a, **kw: ([], None, None)

        with mock.patch.dict(
            "sys.modules",
            {"lib.metrics": mock.MagicMock(), "lib.metrics.aggregator": fake_aggregator},
        ):
            body = delivery_mod.generate_pr_body(self.repo, cfg, state)

        self.assertIn("enabled-change", body)
        self.assertNotIn("disabled-change", body)
        self.assertIn("$0.25", body)
        self.assertIn("Total estimated cost", body)
        # Total cost must be only the enabled change's cost
        self.assertIn("$0.25", body)

    # -- Delivery skipped when disabled -----------------------------------

    def test_attempt_pr_delivery_skips_when_create_pr_false(self) -> None:
        cfg = self._cfg_with_pr_delivery(create_pr=False)
        state = self._state_with_delivery(branch_name="opsx/test-plan")
        with mock.patch.object(
            delivery_mod, "push_delivery_branch",
        ) as mock_push:
            ok, err = delivery_mod.attempt_pr_delivery(
                self.repo, cfg, state,
            )
        self.assertTrue(ok)
        mock_push.assert_not_called()

    # -- Default delivery state includes pull_request_url -----------------

    def test_default_git_delivery_state_includes_pull_request_url(self) -> None:
        state = state_mod._default_git_delivery_state()
        self.assertIn("pull_request_url", state)
        self.assertIsNone(state["pull_request_url"])
