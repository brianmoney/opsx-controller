from __future__ import annotations

import argparse
import subprocess
import tempfile
import textwrap
import unittest
from pathlib import Path
from unittest import mock

from lib.orchestrator import logs as logs_mod
from lib.orchestrator import state as state_mod
from lib.orchestrator import base as base_mod

# Entrypoint module loaded for cmd_logs / main references.
import importlib.util
import os

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


def _load_entrypoint():
    spec = importlib.util.spec_from_file_location("opsx_plan", _SCRIPT)
    assert spec is not None
    assert spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class LogsCommandTests(unittest.TestCase):
    """Tests for ``opsx-plan logs`` command: log selection, filtering,
    listing, follow-mode selection, and missing-log handling."""

    def setUp(self) -> None:
        self.opsx_plan = _load_entrypoint()
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
        self.cid = "add-logs-test"
        self.plan_name = "run-add-logs-test"
        self.cfg = {
            "name": self.plan_name,
            "adapter": "opencode",
            "implement_invoke": "opencode run --agent opsx-implementer",
            "review_invoke": "opencode run --agent opsx-reviewer",
            "archive_invoke": "opencode run --agent opsx-archiver",
            "invoke": 'opencode run "/opsx-drive {change}"',
            "state_file": ".opencode/opsx-controller/{change}.json",
            "timeout_minutes": 1,
            "max_attempts": 2,
            "max_rounds": 2,
            "no_progress_limit": 2,
            "fast_checks": [],
            "check_timeout_minutes": 1,
            "require_clean_tracked": False,
            "review_created": False,
            "changes": {
                self.cid: {
                    "id": self.cid,
                    "depends_on": [],
                    "enabled": True,
                    "pause_before": False,
                    "timeout_minutes": 1,
                    "max_attempts": 2,
                    "create_invoke": "",
                    "create_max_attempts": 1,
                }
            },
            "order": [self.cid],
            "created_check": "",
            "plan_doc": "",
            "create_timeout_minutes": 1,
        }
        self._write_plan_toml()

        self.log_dir = self.repo / ".opsx-plan" / "logs"
        self.log_dir.mkdir(parents=True)

    def tearDown(self) -> None:
        self.tmp.cleanup()

    def _write_plan_toml(self) -> None:
        plan_dir = self.repo
        toml_path = plan_dir / f"{self.plan_name}.toml"
        toml_path.write_text(
            textwrap.dedent(f"""\
                [plan]
                name = "{self.plan_name}"
                adapter = "opencode"
                implement_invoke = "opencode run --agent opsx-implementer"
                review_invoke = "opencode run --agent opsx-reviewer"
                archive_invoke = "opencode run --agent opsx-archiver"

                [[changes]]
                id = "{self.cid}"
            """),
            encoding="utf-8",
        )
        self.plan_path = toml_path

    def _make_log(self, filename: str, content: str = "log content\n") -> Path:
        p = self.log_dir / filename
        p.write_text(content, encoding="utf-8")
        return p

    # -- 3.1 Default latest-log selection from recorded state metadata --------

    def test_default_selects_log_from_state_last_stage(self) -> None:
        log = self._make_log(f"{self.cid}.implement.r1.1.log", "implement output\n")
        state = state_mod.load_state(self.repo, self.plan_name)
        rec = state_mod.rec(state, self.cid)
        rec["last_stage"] = {
            "name": "implement",
            "round": 1,
            "outcome": "exited",
            "log_path": str(log),
            "updated_at": base_mod.utcnow(),
        }
        state_mod.save_state(self.repo, self.plan_name, state)

        selected = logs_mod._select_log_from_state(
            self.repo, self.plan_name, None, None,
        )
        self.assertIsNotNone(selected)
        self.assertIn("implement", selected["path"])
        self.assertEqual(selected["change"], self.cid)
        self.assertEqual(selected["stage"], "implement")

    def test_state_selection_ignores_deleted_log(self) -> None:
        log = self._make_log(f"{self.cid}.review.r1.1.log", "review notes\n")
        state = state_mod.load_state(self.repo, self.plan_name)
        rec = state_mod.rec(state, self.cid)
        rec["last_stage"] = {
            "name": "review",
            "round": 1,
            "outcome": "exited",
            "log_path": str(log),
            "updated_at": base_mod.utcnow(),
        }
        state_mod.save_state(self.repo, self.plan_name, state)

        log.unlink()

        selected = logs_mod._select_log_from_state(
            self.repo, self.plan_name, None, None,
        )
        self.assertIsNone(selected)

    def test_state_selection_picks_newest_across_multi_change(self) -> None:
        older_log = self._make_log("older-change.implement.r1.1.log", "older\n")
        newer_log = self._make_log("newer-change.implement.r2.1.log", "newer\n")

        state = state_mod.load_state(self.repo, self.plan_name)

        rec_older = state_mod.rec(state, "older-change")
        rec_older["last_stage"] = {
            "name": "implement",
            "round": 1,
            "outcome": "exited",
            "log_path": str(older_log),
            "updated_at": base_mod.utcnow(),
        }

        rec_newer = state_mod.rec(state, "newer-change")
        rec_newer["last_stage"] = {
            "name": "implement",
            "round": 2,
            "outcome": "exited",
            "log_path": str(newer_log),
            "updated_at": base_mod.utcnow(),
        }

        state_mod.save_state(self.repo, self.plan_name, state)

        selected = logs_mod._select_log_from_state(
            self.repo, self.plan_name, None, None,
        )
        self.assertIsNotNone(selected)
        self.assertEqual(selected["change"], "newer-change")
        self.assertEqual(selected["stage"], "implement")
        self.assertEqual(selected["round"], 2)

        selected = logs_mod._select_log_from_state(
            self.repo, self.plan_name, "older-change", None,
        )
        self.assertIsNotNone(selected)
        self.assertEqual(selected["change"], "older-change")
        self.assertEqual(selected["round"], 1)

    # -- 3.1a Plan-scoped state selection (regression for out-of-plan state entries) --

    def test_state_selection_scoped_to_plan_change_ids(self) -> None:
        plan_cid = self.cid
        plan_change_ids = {plan_cid}
        plan_log = self._make_log(f"{plan_cid}.implement.r1.1.log", "plan\n")
        foreign_log = self._make_log("foreign-change.implement.r1.1.log", "foreign\n")

        state = state_mod.load_state(self.repo, self.plan_name)

        rec_plan = state_mod.rec(state, plan_cid)
        rec_plan["last_stage"] = {
            "name": "implement",
            "round": 1,
            "outcome": "exited",
            "log_path": str(plan_log),
            "updated_at": base_mod.utcnow(),
        }

        rec_foreign = state_mod.rec(state, "foreign-change")
        rec_foreign["last_stage"] = {
            "name": "implement",
            "round": 1,
            "outcome": "exited",
            "log_path": str(foreign_log),
            "updated_at": base_mod.utcnow(),
        }

        state_mod.save_state(self.repo, self.plan_name, state)

        selected = logs_mod._select_log_from_state(
            self.repo, self.plan_name, None, None,
            plan_change_ids=plan_change_ids,
        )
        self.assertIsNotNone(selected)
        self.assertEqual(selected["change"], plan_cid)

    def test_state_selection_with_explicit_change_bypasses_plan_scoping(self) -> None:
        plan_change_ids = {self.cid}
        foreign_log = self._make_log("foreign-change.implement.r1.1.log", "foreign\n")

        state = state_mod.load_state(self.repo, self.plan_name)
        rec = state_mod.rec(state, "foreign-change")
        rec["last_stage"] = {
            "name": "implement",
            "round": 1,
            "outcome": "exited",
            "log_path": str(foreign_log),
            "updated_at": base_mod.utcnow(),
        }
        state_mod.save_state(self.repo, self.plan_name, state)

        selected = logs_mod._select_log_from_state(
            self.repo, self.plan_name, "foreign-change", None,
            plan_change_ids=plan_change_ids,
        )
        self.assertIsNotNone(selected)
        self.assertEqual(selected["change"], "foreign-change")

    # -- 3.2 Fallback latest-log selection by log-directory ordering -----------

    def test_fallback_selects_newest_log_by_mtime(self) -> None:
        older = self._make_log(f"{self.cid}.implement.r1.1.log", "older\n")
        import time as _time
        _time.sleep(0.01)
        new_path = self._make_log(f"{self.cid}.review.r1.1.log", "newer\n")

        selected = logs_mod._select_log_from_directory(
            self.repo, None, None,
        )
        self.assertIsNotNone(selected)
        self.assertIn("review", selected["path"])
        self.assertEqual(selected["change"], self.cid)
        self.assertEqual(selected["stage"], "review")

    def test_fallback_handles_empty_log_dir(self) -> None:
        selected = logs_mod._select_log_from_directory(
            self.repo, None, None,
        )
        self.assertIsNone(selected)

    def test_fallback_ignores_non_log_files(self) -> None:
        (self.log_dir / "readme.txt").write_text("not a log\n", encoding="utf-8")
        selected = logs_mod._select_log_from_directory(
            self.repo, None, None,
        )
        self.assertIsNone(selected)

    # -- 3.3 Change-id and stage filter combinations ---------------------------

    def test_filter_by_change_id_only(self) -> None:
        self._make_log("change-a.implement.r1.1.log", "a\n")
        self._make_log("change-b.implement.r1.1.log", "b\n")

        selected = logs_mod._select_log(
            self.repo, self.plan_name, "change-a", None,
        )
        self.assertIsNotNone(selected)
        self.assertEqual(selected["change"], "change-a")

    def test_filter_by_stage_only(self) -> None:
        self._make_log(f"{self.cid}.implement.r1.1.log", "impl\n")
        self._make_log(f"{self.cid}.review.r1.1.log", "rev\n")

        selected = logs_mod._select_log(
            self.repo, self.plan_name, None, "review",
        )
        self.assertIsNotNone(selected)
        self.assertEqual(selected["stage"], "review")

    def test_filter_by_change_and_stage(self) -> None:
        self._make_log("change-a.review.r1.1.log", "a rev\n")
        self._make_log("change-b.review.r1.1.log", "b rev\n")

        selected = logs_mod._select_log(
            self.repo, self.plan_name, "change-b", "review",
        )
        self.assertIsNotNone(selected)
        self.assertEqual(selected["change"], "change-b")
        self.assertEqual(selected["stage"], "review")

    def test_filters_applied_across_state_and_directory(self) -> None:
        log = self._make_log("change-x.implement.r1.1.log", "x\n")
        state = state_mod.load_state(self.repo, self.plan_name)
        rec = state_mod.rec(state, "change-x")
        rec["last_stage"] = {
            "name": "implement",
            "round": 1,
            "outcome": "exited",
            "log_path": str(log),
            "updated_at": base_mod.utcnow(),
        }
        state_mod.save_state(self.repo, self.plan_name, state)

        selected = logs_mod._select_log(
            self.repo, self.plan_name, "change-x", "implement",
        )
        self.assertIsNotNone(selected)

        selected = logs_mod._select_log(
            self.repo, self.plan_name, "no-such-change", None,
        )
        self.assertIsNone(selected)

    # -- 3.4 List output and follow-mode target selection ----------------------

    def test_list_output_shows_matching_logs(self) -> None:
        self._make_log(f"{self.cid}.implement.r1.1.log", "impl\n")
        self._make_log(f"{self.cid}.review.r1.1.log", "rev\n")
        self._make_log("other-change.implement.r1.1.log", "other\n")

        entries = logs_mod._collect_filtered_logs(
            self.repo, self.cid, None,
        )
        self.assertEqual(len(entries), 2)
        for e in entries:
            self.assertEqual(e["change"], self.cid)

    def test_list_output_empty_when_no_match(self) -> None:
        entries = logs_mod._collect_filtered_logs(
            self.repo, "nonexistent", None,
        )
        self.assertEqual(len(entries), 0)

    # -- 3.4a Plan-scoped directory fallback (regression for out-of-plan logs) --

    def test_directory_fallback_scoped_to_plan_change_ids(self) -> None:
        plan_cid = self.cid
        plan_change_ids = {plan_cid}
        self._make_log(f"{plan_cid}.implement.r1.1.log", "plan log\n")
        self._make_log("other-plan-change.implement.r1.1.log", "foreign log\n")

        selected = logs_mod._select_log_from_directory(
            self.repo, None, None, plan_change_ids=plan_change_ids,
        )
        self.assertIsNotNone(selected)
        self.assertEqual(selected["change"], plan_cid)

    def test_directory_fallback_with_change_filter_still_exact(self) -> None:
        plan_change_ids = {self.cid}
        foreign_log = self._make_log("foreign-change.implement.r1.1.log", "foreign\n")

        selected = logs_mod._select_log_from_directory(
            self.repo, "foreign-change", None, plan_change_ids=plan_change_ids,
        )
        self.assertIsNotNone(selected)
        self.assertEqual(selected["change"], "foreign-change")

    def test_directory_fallback_no_plan_logs_returns_none(self) -> None:
        plan_change_ids = {self.cid}
        self._make_log("foreign-change.review.r1.1.log", "foreign\n")

        selected = logs_mod._select_log_from_directory(
            self.repo, None, None, plan_change_ids=plan_change_ids,
        )
        self.assertIsNone(selected)

    def test_list_collection_scoped_to_plan_change_ids(self) -> None:
        plan_cid = self.cid
        plan_change_ids = {plan_cid}
        self._make_log(f"{plan_cid}.implement.r1.1.log", "plan\n")
        self._make_log(f"{plan_cid}.review.r1.1.log", "plan review\n")
        self._make_log("other-change.implement.r1.1.log", "other\n")

        entries = logs_mod._collect_filtered_logs(
            self.repo, None, None, plan_change_ids=plan_change_ids,
        )
        self.assertEqual(len(entries), 2)
        for e in entries:
            self.assertEqual(e["change"], plan_cid)

    def test_list_collection_with_change_filter_ignores_plan_scoping(self) -> None:
        plan_change_ids = {self.cid}
        self._make_log("foreign.implement.r1.1.log", "foreign\n")

        entries = logs_mod._collect_filtered_logs(
            self.repo, "foreign", None, plan_change_ids=plan_change_ids,
        )
        self.assertEqual(len(entries), 1)
        self.assertEqual(entries[0]["change"], "foreign")

    def test_cmd_logs_list_mode_excludes_out_of_plan_logs(self) -> None:
        self._make_log(f"{self.cid}.implement.r1.1.log", "plan log\n")
        self._make_log("other-plan-change.review.r1.1.log", "foreign log\n")

        args = argparse.Namespace(
            repo=str(self.repo),
            plan=str(self.plan_path),
            change=None,
            stage=None,
            list=True,
            follow=False,
        )
        rc = self.opsx_plan.cmd_logs(args)
        self.assertEqual(rc, 0)

    def test_cmd_logs_default_excludes_out_of_plan_logs(self) -> None:
        self._make_log("foreign-change.implement.r1.1.log", "foreign\n")

        args = argparse.Namespace(
            repo=str(self.repo),
            plan=str(self.plan_path),
            change=None,
            stage=None,
            list=False,
            follow=False,
        )
        rc = self.opsx_plan.cmd_logs(args)
        self.assertEqual(rc, 1)

    def test_follow_mode_selects_same_log_as_default(self) -> None:
        log = self._make_log(f"{self.cid}.implement.r1.1.log", "in progress\n")
        log.touch()

        state = state_mod.load_state(self.repo, self.plan_name)
        rec = state_mod.rec(state, self.cid)
        rec["last_stage"] = {
            "name": "implement",
            "round": 1,
            "outcome": "exited",
            "log_path": str(log),
            "updated_at": base_mod.utcnow(),
        }
        state_mod.save_state(self.repo, self.plan_name, state)

        selected = logs_mod._select_log(
            self.repo, self.plan_name, None, None,
        )
        self.assertIsNotNone(selected)
        self.assertEqual(selected["stage"], "implement")

    # -- 3.5 Missing-log handling ----------------------------------------------

    def test_select_log_returns_none_when_no_logs_exist(self) -> None:
        selected = logs_mod._select_log(
            self.repo, self.plan_name, None, None,
        )
        self.assertIsNone(selected)

    def test_select_log_returns_none_when_filter_matches_nothing(self) -> None:
        self._make_log(f"{self.cid}.implement.r1.1.log", "impl\n")

        selected = logs_mod._select_log(
            self.repo, self.plan_name, None, "archive",
        )
        self.assertIsNone(selected)

    def test_cmd_logs_exits_nonzero_for_missing_log(self) -> None:
        args = argparse.Namespace(
            repo=str(self.repo),
            plan=str(self.plan_path),
            change=None,
            stage=None,
            list=False,
            follow=False,
        )
        rc = self.opsx_plan.cmd_logs(args)
        self.assertEqual(rc, 1)

    # -- CLI dispatch ----------------------------------------------------------

    def test_logs_subcommand_routes_to_cmd_logs(self) -> None:
        calls: list[argparse.Namespace] = []

        def fake_cmd_logs(args: argparse.Namespace) -> int:
            calls.append(args)
            return 42

        with mock.patch.object(
            self.opsx_plan, "cmd_logs", side_effect=fake_cmd_logs
        ) as cmd_logs, mock.patch.object(
            self.opsx_plan.sys,
            "argv",
            ["opsx-plan", "--repo", str(self.repo),
             "logs", str(self.plan_path)],
        ):
            rc = self.opsx_plan.main()
        self.assertEqual(rc, 42)
        cmd_logs.assert_called_once()

    def test_logs_list_mode_cli(self) -> None:
        self._make_log(f"{self.cid}.implement.r1.1.log", "impl\n")

        args = argparse.Namespace(
            repo=str(self.repo),
            plan=str(self.plan_path),
            change=None,
            stage=None,
            list=True,
            follow=False,
        )
        rc = self.opsx_plan.cmd_logs(args)
        self.assertEqual(rc, 0)

    # -- Legacy log filename pattern -------------------------------------------

    def test_parse_legacy_log_name(self) -> None:
        result = logs_mod._parse_log_name("change-a.drive1.log")
        self.assertIsNotNone(result)
        self.assertEqual(result["change"], "change-a")
        self.assertEqual(result["stage"], "drive")
        self.assertEqual(result["round"], 0)
        self.assertEqual(result["seq"], 1)

    def test_parse_direct_log_name(self) -> None:
        result = logs_mod._parse_log_name("change-a.implement.r2.3.log")
        self.assertIsNotNone(result)
        self.assertEqual(result["change"], "change-a")
        self.assertEqual(result["stage"], "implement")
        self.assertEqual(result["round"], 2)
        self.assertEqual(result["seq"], 3)

    def test_parse_unknown_log_name_returns_none(self) -> None:
        result = logs_mod._parse_log_name("not-a-log.txt")
        self.assertIsNone(result)
