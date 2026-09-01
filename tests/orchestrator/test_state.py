from __future__ import annotations

import importlib.util
import sys
import json
import os
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from lib.orchestrator import state as state_mod
from lib.orchestrator import base as base_mod

_SCRIPT = Path(__file__).resolve().parents[2] / "orchestrator" / "opsx-plan.py"

_MODEL_HOME: tempfile.TemporaryDirectory | None = None
_MODEL_CONFIG_PATCH = None
_MODEL_ENV_PATCH = None
from lib.models import resolver


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


class GitDeliveryStatePersistenceTests(unittest.TestCase):
    def setUp(self) -> None:
        self.opsx_plan = load_opsx_plan()
        self.tmp = tempfile.TemporaryDirectory()
        self.repo = Path(self.tmp.name)

    def tearDown(self) -> None:
        self.tmp.cleanup()

    def test_load_state_adds_default_git_delivery(self) -> None:
        state = state_mod.load_state(self.repo, "test-plan")
        self.assertIn("git_delivery", state)
        gd = state["git_delivery"]
        self.assertEqual(gd["base_ref"], None)
        self.assertEqual(gd["branch_name"], None)
        self.assertEqual(gd["delivery_status"], "disabled")

    def test_load_state_preserves_existing_git_delivery(self) -> None:
        state_path = state_mod.state_path(self.repo, "test-plan")
        state_path.parent.mkdir(parents=True, exist_ok=True)
        existing = {
            "plan": "test-plan",
            "approvals": [],
            "changes": {},
            "git_delivery": {
                "base_ref": "main",
                "branch_name": "opsx/test-plan",
                "delivery_status": "branch_ready",
            },
        }
        state_path.write_text(json.dumps(existing), encoding="utf-8")
        state = state_mod.load_state(self.repo, "test-plan")
        gd = state["git_delivery"]
        self.assertEqual(gd["base_ref"], "main")
        self.assertEqual(gd["branch_name"], "opsx/test-plan")
        self.assertEqual(gd["delivery_status"], "branch_ready")

    def test_load_state_merges_missing_git_delivery_keys(self) -> None:
        state_path = state_mod.state_path(self.repo, "test-plan")
        state_path.parent.mkdir(parents=True, exist_ok=True)
        existing = {
            "plan": "test-plan",
            "approvals": [],
            "changes": {},
            "git_delivery": {"base_ref": "release/next"},
        }
        state_path.write_text(json.dumps(existing), encoding="utf-8")
        state = state_mod.load_state(self.repo, "test-plan")
        gd = state["git_delivery"]
        self.assertEqual(gd["base_ref"], "release/next")
        self.assertEqual(gd["branch_name"], None)
        self.assertEqual(gd["delivery_status"], "disabled")

    def test_load_state_handles_non_dict_git_delivery(self) -> None:
        state_path = state_mod.state_path(self.repo, "test-plan")
        state_path.parent.mkdir(parents=True, exist_ok=True)
        existing = {
            "plan": "test-plan",
            "approvals": [],
            "changes": {},
            "git_delivery": "bad-value",
        }
        state_path.write_text(json.dumps(existing), encoding="utf-8")
        state = state_mod.load_state(self.repo, "test-plan")
        gd = state["git_delivery"]
        self.assertIsInstance(gd, dict)
        self.assertEqual(gd["delivery_status"], "disabled")


class EscalationStateMigrationTests(unittest.TestCase):
    """Verify that pre-escalation state files are migrated with defaults."""

    def setUp(self) -> None:
        self.opsx_plan = load_opsx_plan()
        self.tmp = tempfile.TemporaryDirectory()
        self.repo = Path(self.tmp.name)

    def tearDown(self) -> None:
        self.tmp.cleanup()

    def test_load_state_adds_default_escalation_fields(self) -> None:
        """Load a state file that lacks the escalation key; verify defaults."""
        state_path = state_mod.state_path(self.repo, "test-plan")
        state_path.parent.mkdir(parents=True, exist_ok=True)
        existing = {
            "plan": "test-plan",
            "approvals": [],
            "changes": {
                "c1": {
                    "status": base_mod.DONE,
                    "phase": "done",
                    "round": 2,
                    "max_rounds": 3,
                    "no_progress_streak": 0,
                    "latest_fix_prompt": "",
                    "last_result": "review_passed",
                    "task_counts": {"complete": 2, "total": 2},
                    "tracked_change_files": [],
                    "context_cache": state_mod.default_context_cache(),
                    "last_review": state_mod.default_last_review(),
                    "archive": state_mod.default_archive_state(),
                    "history": [],
                    "telemetry": {"latest_telemetry": ""},
                    "change": "c1",
                    "attempts": 0,
                    "reason": "",
                    "updated_at": "",
                    "create_attempts": 0,
                    "created_by_orchestrator": False,
                    "accepted": False,
                    "last_stage": state_mod.default_last_stage(),
                    "last_log": "",
                    # NOTE: no "escalation" key — pre-escalation state
                }
            },
        }
        state_path.write_text(json.dumps(existing), encoding="utf-8")
        state = state_mod.load_state(self.repo, "test-plan")
        rec = state["changes"]["c1"]
        self.assertIn("escalation", rec,
                      "merge_defaults must add escalation key")
        esc = rec["escalation"]
        self.assertIsInstance(esc, dict)
        self.assertFalse(esc["active"])
        self.assertEqual(esc["activated_round"], 0)
        self.assertEqual(esc["model"], "")

    def test_load_state_preserves_existing_escalation_fields(self) -> None:
        """Load a state file that already has active escalation; verify preserved."""
        state_path = state_mod.state_path(self.repo, "test-plan")
        state_path.parent.mkdir(parents=True, exist_ok=True)
        existing = {
            "plan": "test-plan",
            "approvals": [],
            "changes": {
                "c1": {
                    "status": base_mod.RUNNING,
                    "phase": "implement",
                    "round": 4,
                    "max_rounds": 5,
                    "no_progress_streak": 0,
                    "latest_fix_prompt": "",
                    "last_result": "",
                    "task_counts": {"complete": 4, "total": 10},
                    "tracked_change_files": [],
                    "context_cache": state_mod.default_context_cache(),
                    "last_review": state_mod.default_last_review(),
                    "archive": state_mod.default_archive_state(),
                    "history": [],
                    "telemetry": {"latest_telemetry": ""},
                    "escalation": {
                        "active": True,
                        "activated_round": 3,
                        "model": "deepseek/v4-ultra",
                    },
                    "change": "c1",
                    "attempts": 0,
                    "reason": "",
                    "updated_at": "",
                    "create_attempts": 0,
                    "created_by_orchestrator": False,
                    "accepted": False,
                    "last_stage": state_mod.default_last_stage(),
                    "last_log": "",
                }
            },
        }
        state_path.write_text(json.dumps(existing), encoding="utf-8")
        state = state_mod.load_state(self.repo, "test-plan")
        esc = state["changes"]["c1"]["escalation"]
        self.assertTrue(esc["active"])
        self.assertEqual(esc["activated_round"], 3)
        self.assertEqual(esc["model"], "deepseek/v4-ultra")


class TaskClassificationTests(unittest.TestCase):
    """Manual/automatable task classification and completeness helpers."""

    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.repo = Path(self.tmp.name)

    def tearDown(self) -> None:
        self.tmp.cleanup()

    def _write_tasks(self, cid: str, text: str) -> None:
        cdir = self.repo / "openspec" / "changes" / cid
        cdir.mkdir(parents=True)
        (cdir / "tasks.md").write_text(text, encoding="utf-8")

    def test_classify_task_line_marker_case_and_whitespace(self) -> None:
        self.assertEqual(base_mod.classify_task_line("- [ ] 1.1 do it (manual)"), "manual")
        self.assertEqual(base_mod.classify_task_line("- [ ] 1.1 do it (MANUAL)  "), "manual")
        self.assertEqual(base_mod.classify_task_line("- [x] 1.1 do it (manual)"), "manual")
        self.assertEqual(base_mod.classify_task_line("- [ ] 1.2 add a test"), "automatable")
        self.assertEqual(base_mod.classify_task_line("- [ ] 1.3 write (manual) coverage"), "automatable")

    def test_change_tasks_classifies_marked_and_unmarked_lines(self) -> None:
        self._write_tasks(
            "add-thing",
            "## 1\n\n"
            "- [ ] 1.1 Plant a malformed artifact and run the live jobs (manual)\n"
            "- [ ] 1.2 verify in prod (MANUAL)  \n"
            "- [ ] 1.3 Add a regression test\n",
        )
        tasks = state_mod.change_tasks(self.repo, "add-thing")
        self.assertEqual(
            [t["id"] for t in tasks],
            [
                "1.1 Plant a malformed artifact and run the live jobs (manual)",
                "1.2 verify in prod (MANUAL)",
                "1.3 Add a regression test",
            ],
        )
        self.assertEqual(
            [t["manual"] for t in tasks],
            [True, True, False],
        )
        self.assertEqual([t["done"] for t in tasks], [False, False, False])

    def test_remaining_automatable_excludes_manual_and_checked(self) -> None:
        self._write_tasks(
            "add-thing",
            "## 1\n\n"
            "- [x] 1.1 Add a regression test\n"
            "- [ ] 1.2 Manual follow-up (manual)\n"
            "- [ ] 1.3 Finish the wiring\n",
        )
        self.assertEqual(
            state_mod.remaining_automatable_tasks(self.repo, "add-thing"),
            ["1.3 Finish the wiring"],
        )
        self.assertEqual(
            state_mod.pending_manual_tasks(self.repo, "add-thing"),
            ["1.2 Manual follow-up (manual)"],
        )

    def test_remaining_automatable_all_manual_unchecked(self) -> None:
        self._write_tasks(
            "add-thing",
            "## 1\n\n"
            "- [x] 1.1 Add a regression test\n"
            "- [ ] 1.2 Plant fixtures (manual)\n",
        )
        self.assertEqual(state_mod.remaining_automatable_tasks(self.repo, "add-thing"), [])
        self.assertEqual(state_mod.pending_manual_tasks(self.repo, "add-thing"), ["1.2 Plant fixtures (manual)"])

    def test_change_task_counts_totals_all_tasks(self) -> None:
        self._write_tasks(
            "add-thing",
            "## 1\n\n"
            "- [x] 1.1 Add a regression test\n"
            "- [ ] 1.2 Manual follow-up (manual)\n",
        )
        self.assertEqual(
            state_mod.change_task_counts(self.repo, "add-thing"),
            {"complete": 1, "total": 2},
        )

    def test_helpers_return_empty_when_tasks_missing(self) -> None:
        self.assertEqual(state_mod.change_tasks(self.repo, "missing"), [])
        self.assertEqual(state_mod.remaining_automatable_tasks(self.repo, "missing"), [])
        self.assertEqual(state_mod.pending_manual_tasks(self.repo, "missing"), [])


if __name__ == "__main__":
    unittest.main()
