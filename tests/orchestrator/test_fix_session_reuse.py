"""Tests for opt-in warm implement FIX rounds (``reuse_fix_sessions``).

Covers the OpenCode session-id capture boundary, the four-condition gate that
decides when a fix round resumes a session, the argv plumbing in
``invoke_direct_stage``, and the fail-open cold retry when a stored session id
no longer exists.
"""

from __future__ import annotations

import importlib.util
import json
import subprocess
import sys
import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path
from unittest import mock

from lib.orchestrator import state as state_mod

SCRIPT = Path(__file__).resolve().parents[2] / "orchestrator" / "opsx-plan.py"


def load_opsx_plan():
    spec = importlib.util.spec_from_file_location("opsx_plan", SCRIPT)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules["opsx_plan"] = module
    spec.loader.exec_module(module)
    return module


def git(repo: Path, *args: str) -> None:
    subprocess.run(
        ["git", *args], cwd=repo, check=True, capture_output=True, text=True
    )


class _FakeProc:
    def __init__(self, returncode: int = 0, stdout: str = "") -> None:
        self.returncode = returncode
        self.stdout = stdout


class SessionCaptureTests(unittest.TestCase):
    """``capture_opencode_session_id`` filtering, staleness, and tolerance."""

    def setUp(self) -> None:
        self.opsx_plan = load_opsx_plan()
        self.tmp = tempfile.TemporaryDirectory()
        self.repo = Path(self.tmp.name).resolve()
        self.start = datetime(2026, 9, 19, 10, 0, 0, tzinfo=timezone.utc)
        self.since = self.start.isoformat()
        self.start_ms = int(self.start.timestamp() * 1000)

    def tearDown(self) -> None:
        self.tmp.cleanup()

    def _runner(self, entries, returncode=0):
        calls = []

        def runner(cmd, **kwargs):
            calls.append((cmd, kwargs))
            return _FakeProc(returncode, json.dumps(entries))

        runner.calls = calls
        return runner

    def test_returns_newest_session_for_this_directory(self) -> None:
        entries = [
            {
                "id": "ses_old",
                "directory": str(self.repo),
                "created": self.start_ms + 1000,
            },
            {
                "id": "ses_new",
                "directory": str(self.repo),
                "created": self.start_ms + 9000,
            },
            {
                "id": "ses_other",
                "directory": "/some/other/repo",
                "created": self.start_ms + 50000,
            },
        ]
        captured = self.opsx_plan.capture_opencode_session_id(
            self.repo, self.since, runner=self._runner(entries)
        )
        self.assertEqual(captured, "ses_new")

    def test_stale_session_created_before_dispatch_is_ignored(self) -> None:
        entries = [
            {
                "id": "ses_stale",
                "directory": str(self.repo),
                "created": self.start_ms - 1,
            }
        ]
        captured = self.opsx_plan.capture_opencode_session_id(
            self.repo, self.since, runner=self._runner(entries)
        )
        self.assertIsNone(captured)

    def test_other_directory_is_filtered_out(self) -> None:
        entries = [
            {
                "id": "ses_other",
                "directory": "/some/other/repo",
                "created": self.start_ms + 1000,
            }
        ]
        captured = self.opsx_plan.capture_opencode_session_id(
            self.repo, self.since, runner=self._runner(entries)
        )
        self.assertIsNone(captured)

    def test_nonzero_exit_is_non_fatal(self) -> None:
        captured = self.opsx_plan.capture_opencode_session_id(
            self.repo, self.since, runner=self._runner([], returncode=1)
        )
        self.assertIsNone(captured)

    def test_runner_exception_is_non_fatal(self) -> None:
        def boom(cmd, **kwargs):
            raise OSError("opencode not on PATH")

        captured = self.opsx_plan.capture_opencode_session_id(
            self.repo, self.since, runner=boom
        )
        self.assertIsNone(captured)

    def test_unparseable_json_is_non_fatal(self) -> None:
        def runner(cmd, **kwargs):
            return _FakeProc(0, "not json at all")

        captured = self.opsx_plan.capture_opencode_session_id(
            self.repo, self.since, runner=runner
        )
        self.assertIsNone(captured)


class FixRoundSessionGateTests(unittest.TestCase):
    """The four-condition gate: key + adapter + fix + stored id."""

    def setUp(self) -> None:
        self.opsx_plan = load_opsx_plan()
        self.cid = "c1"

    def _cfg(self, **overrides):
        cfg = {"adapter": "opencode", "reuse_fix_sessions": True}
        cfg.update(overrides)
        return cfg

    def _state(self, **record_overrides):
        state = {"plan": "p", "approvals": [], "changes": {}}
        r = state_mod.rec(state, self.cid)
        r["phase"] = "implement"
        r["latest_fix_prompt"] = "CHANGE: c1\nFINDINGS: ..."
        r["worker_sessions"] = {"implement": "ses_abc"}
        r.update(record_overrides)
        return state

    def test_returns_session_when_all_conditions_hold(self) -> None:
        got = self.opsx_plan._fix_round_session_id(
            self._cfg(), self._state(), self.cid
        )
        self.assertEqual(got, "ses_abc")

    def test_key_off_returns_none(self) -> None:
        got = self.opsx_plan._fix_round_session_id(
            self._cfg(reuse_fix_sessions=False), self._state(), self.cid
        )
        self.assertIsNone(got)

    def test_key_absent_defaults_off(self) -> None:
        cfg = {"adapter": "opencode"}
        got = self.opsx_plan._fix_round_session_id(cfg, self._state(), self.cid)
        self.assertIsNone(got)

    def test_non_opencode_adapter_returns_none(self) -> None:
        got = self.opsx_plan._fix_round_session_id(
            self._cfg(adapter="claude-code"), self._state(), self.cid
        )
        self.assertIsNone(got)

    def test_non_fix_round_returns_none(self) -> None:
        state = self._state(latest_fix_prompt="")
        got = self.opsx_plan._fix_round_session_id(self._cfg(), state, self.cid)
        self.assertIsNone(got)

    def test_non_implement_phase_returns_none(self) -> None:
        state = self._state(phase="review")
        got = self.opsx_plan._fix_round_session_id(self._cfg(), state, self.cid)
        self.assertIsNone(got)

    def test_missing_stored_session_returns_none(self) -> None:
        state = self._state(worker_sessions={})
        got = self.opsx_plan._fix_round_session_id(self._cfg(), state, self.cid)
        self.assertIsNone(got)


class InvokeDirectStageSessionTests(unittest.TestCase):
    """``invoke_direct_stage`` appends ``--session`` only for opencode implement."""

    def setUp(self) -> None:
        self.opsx_plan = load_opsx_plan()
        self.tmp = tempfile.TemporaryDirectory()
        self.repo = Path(self.tmp.name)
        self.cid = "c1"

    def tearDown(self) -> None:
        self.tmp.cleanup()

    def _cfg(self, adapter="opencode"):
        return {
            "adapter": adapter,
            "implement_invoke": "opencode run --agent opsx-implementer",
            "review_invoke": "opencode run --agent opsx-reviewer",
            "changes": {self.cid: {"timeout_minutes": 1}},
        }

    def _capture_cmd(self, **invoke_kwargs):
        captured = []

        def fake_run_logged_command(
            repo, cmd, log_path, timeout_s, stage, attempt, input_text=""
        ):
            captured.append(cmd)
            return "exited", log_path

        with mock.patch.object(
            self.opsx_plan,
            "run_logged_command",
            side_effect=fake_run_logged_command,
        ):
            outcome, _log = self.opsx_plan.invoke_direct_stage(
                self.repo,
                self._cfg(invoke_kwargs.pop("adapter", "opencode")),
                self.cid,
                invoke_kwargs.pop("stage", "implement"),
                1,
                "INPUT",
                **invoke_kwargs,
            )
        self.assertEqual(outcome, "exited")
        return captured[0]

    def test_session_appended_for_opencode_implement(self) -> None:
        cmd = self._capture_cmd(session_id="ses_abc")
        self.assertIn("--session", cmd)
        self.assertEqual(cmd[cmd.index("--session") + 1], "ses_abc")
        self.assertEqual(cmd[-1], "INPUT")

    def test_no_session_flag_without_session_id(self) -> None:
        cmd = self._capture_cmd()
        self.assertNotIn("--session", cmd)
        self.assertEqual(cmd[-1], "INPUT")

    def test_no_session_flag_for_non_implement_stage(self) -> None:
        cmd = self._capture_cmd(session_id="ses_abc", stage="review")
        self.assertNotIn("--session", cmd)

    def test_no_session_flag_for_non_opencode_adapter(self) -> None:
        cmd = self._capture_cmd(session_id="ses_abc", adapter="claude-code")
        self.assertNotIn("--session", cmd)


class FixRoundLoopFallbackTests(unittest.TestCase):
    """A stale stored session triggers exactly one cold redispatch."""

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
        self.cid = "fix-session-change"
        cdir = self.repo / "openspec" / "changes" / self.cid
        cdir.mkdir(parents=True)
        (cdir / "proposal.md").write_text("## Why\n", encoding="utf-8")
        (cdir / "tasks.md").write_text(
            "## 1. Tasks\n\n- [ ] 1.1 Example task\n", encoding="utf-8"
        )
        self.cfg = {
            "name": "session-plan",
            "adapter": "opencode",
            "implement_invoke": "opencode run --agent opsx-implementer",
            "review_invoke": "opencode run --agent opsx-reviewer",
            "archive_invoke": "opencode run --agent opsx-archiver",
            "state_file": ".opsx-plan/workers/{change}.json",
            "reuse_fix_sessions": True,
            "timeout_minutes": 1,
            "max_rounds": 5,
            "no_progress_limit": 2,
            "invalid_output_retries": 0,
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
                    "create_invoke": "",
                    "create_max_attempts": 1,
                }
            },
            "order": [self.cid],
            "created_check": "",
            "plan_doc": "",
            "create_timeout_minutes": 1,
        }
        self.state = {"plan": "session-plan", "approvals": [], "changes": {}}
        r = state_mod.rec(self.state, self.cid)
        r["phase"] = "implement"
        r["round"] = 2
        r["max_rounds"] = 5
        r["latest_fix_prompt"] = "CHANGE: fix-session-change\nFINDINGS: fix it"
        r["worker_sessions"] = {"implement": "ses_stale"}
        self.calls: list[str | None] = []
        self._capture_patch = mock.patch.object(
            self.opsx_plan, "capture_opencode_session_id", return_value=None
        )
        self._capture_patch.start()

    def tearDown(self) -> None:
        self._capture_patch.stop()
        self.tmp.cleanup()

    def _install_fake_invoke(self) -> None:
        def fake_invoke(
            repo, cfg, cid, stage, round_num, input_block, session_id=None
        ):
            self.calls.append(session_id)
            log_path = self.opsx_plan.next_stage_log_path(repo, cid, stage, round_num)
            log_path.parent.mkdir(parents=True, exist_ok=True)
            if session_id:
                log_path.write_text(
                    "Error: Session not found\n", encoding="utf-8"
                )
            else:
                log_path.write_text(
                    json.dumps(
                        {
                            "status": "blocked",
                            "reason": "stopped for the test",
                            "summary": "blocked",
                        }
                    )
                    + "\n",
                    encoding="utf-8",
                )
            return "exited", log_path

        self.opsx_plan.invoke_direct_stage = fake_invoke

    def test_stale_session_redispatches_once_and_clears_id(self) -> None:
        self._install_fake_invoke()
        result = self.opsx_plan.run_direct_change(
            self.repo, self.cfg, self.state, self.cid
        )
        self.assertEqual(result, "stop")
        self.assertEqual(self.calls, ["ses_stale", None])
        record = state_mod.rec(self.state, self.cid)
        self.assertNotIn("implement", record.get("worker_sessions", {}))

    def test_key_off_never_reuses_or_falls_back(self) -> None:
        self.cfg["reuse_fix_sessions"] = False
        # Freshly planted id must survive untouched because no dispatch reads it.
        state = {"plan": "session-plan", "approvals": [], "changes": {}}
        r = state_mod.rec(state, self.cid)
        r["phase"] = "implement"
        r["round"] = 2
        r["max_rounds"] = 5
        r["latest_fix_prompt"] = "CHANGE: fix-session-change\nFINDINGS: fix it"
        r["worker_sessions"] = {"implement": "ses_keep"}
        self._install_fake_invoke()
        result = self.opsx_plan.run_direct_change(
            self.repo, self.cfg, state, self.cid
        )
        self.assertEqual(result, "stop")
        self.assertEqual(self.calls, [None])
        self.assertEqual(
            state_mod.rec(state, self.cid)["worker_sessions"]["implement"],
            "ses_keep",
        )
