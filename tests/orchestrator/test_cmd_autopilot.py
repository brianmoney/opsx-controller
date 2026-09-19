"""Tests for ``lib.orchestrator.cmd_autopilot``.

The engine is faked through the injectable *runner*; the clock is faked
through *now_func*/*sleep_func*.  Every test uses a throwaway git repo and a
minimal plan TOML so plan resolution exercises the real
``planref.resolve_plan``/``load_plan`` path.
"""

from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

from lib.models import resolver
from lib.orchestrator import base, cmd_autopilot
from lib.orchestrator import state as state_mod

PLAN_NAME = "autopilot-test"
CHANGE_ID = "c1"
EXE = "/fake/opsx-plan"

_MODEL_HOME: tempfile.TemporaryDirectory | None = None
_MODEL_PATCH = None
_ENV_PATCH = None


def setUpModule() -> None:
    """Pin model resolution so plan loading never reads ambient config."""
    global _MODEL_HOME, _MODEL_PATCH, _ENV_PATCH
    _MODEL_HOME = tempfile.TemporaryDirectory()
    _MODEL_PATCH = mock.patch.object(
        resolver, "USER_CONFIG_PATH", Path(_MODEL_HOME.name) / "models.toml"
    )
    _MODEL_PATCH.start()
    _ENV_PATCH = mock.patch.dict(
        os.environ,
        {
            "OPSX_CONTROLLER_MODEL": "test-provider/test-controller",
            "OPSX_IMPLEMENTER_MODEL": "test-provider/test-implementer",
            "OPSX_REVIEWER_MODEL": "test-provider/test-reviewer",
            "OPSX_ARCHIVER_MODEL": "test-provider/test-archiver",
        },
    )
    _ENV_PATCH.start()


def tearDownModule() -> None:
    assert _ENV_PATCH is not None and _MODEL_PATCH is not None and _MODEL_HOME is not None
    _ENV_PATCH.stop()
    _MODEL_PATCH.stop()
    _MODEL_HOME.cleanup()


def git(repo: Path, *args: str) -> None:
    subprocess.run(["git", *args], cwd=repo, check=True, capture_output=True, text=True)


def result(rc: int = 0, stdout: str = "", stderr: str = "") -> SimpleNamespace:
    return SimpleNamespace(returncode=rc, stdout=stdout, stderr=stderr)


def change(cid: str = CHANGE_ID, status: str = "pending", reason: str = "") -> dict:
    return {"id": cid, "status": status, "reason": reason}


def document(*changes: dict) -> dict:
    return {"command": "opsx-plan status", "plan": PLAN_NAME, "changes": list(changes)}


class FakeClock:
    """Deterministic clock; ``sleep`` advances it by the requested seconds."""

    def __init__(self, start: datetime | None = None) -> None:
        self.now = start or datetime(2026, 1, 2, 3, 4, 5, tzinfo=timezone.utc)

    def __call__(self) -> datetime:
        return self.now

    def sleep(self, seconds: float) -> None:
        self.now += timedelta(seconds=float(seconds))


class FakeEngine:
    """Fake ``opsx-plan`` child process.

    ``status_docs`` is consumed one pass at a time; the final element repeats
    once exhausted.
    """

    def __init__(
        self,
        status_docs: list[dict],
        *,
        run_rc: int = 0,
        approve_rc: int = 0,
        reset_rc: int = 0,
        status_rc: int = 0,
        status_raw: str | None = None,
    ) -> None:
        self.status_docs = list(status_docs)
        self.run_rc = run_rc
        self.approve_rc = approve_rc
        self.reset_rc = reset_rc
        self.status_rc = status_rc
        self.status_raw = status_raw
        self.calls: list[list[str]] = []
        self.run_count = 0
        self.approve_count = 0
        self.reset_count = 0

    def _next_doc(self) -> dict:
        if len(self.status_docs) > 1:
            return self.status_docs.pop(0)
        return self.status_docs[0] if self.status_docs else document()

    def __call__(self, cmd, *, cwd, capture, timeout):
        self.calls.append(list(cmd))
        sub = cmd[1] if len(cmd) > 1 else ""
        if sub == "run":
            self.run_count += 1
            return result(self.run_rc)
        if sub == "status":
            if self.status_raw is not None:
                return result(self.status_rc, self.status_raw)
            return result(self.status_rc, json.dumps(self._next_doc()))
        if sub == "approve":
            self.approve_count += 1
            return result(self.approve_rc)
        if sub == "reset":
            self.reset_count += 1
            return result(self.reset_rc)
        return result(0)


def read_jsonl(path: Path) -> list[dict]:
    if not path.is_file():
        return []
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line.strip()]


class AutopilotHarness(unittest.TestCase):
    def setUp(self) -> None:
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.repo = Path(tmp.name)
        git(self.repo, "init")
        (self.repo / "tracked.txt").write_text("base\n", encoding="utf-8")
        git(self.repo, "add", "tracked.txt")
        git(self.repo, "-c", "user.email=t@t", "-c", "user.name=t", "commit", "-m", "init")
        self.plan = self.repo / "plan.toml"
        self.plan.write_text(
            f'[plan]\nname = "{PLAN_NAME}"\nadapter = "opencode"\n\n'
            f'[[changes]]\nid = "{CHANGE_ID}"\n',
            encoding="utf-8",
        )
        self._config_patch = mock.patch.object(
            cmd_autopilot, "_config_path", return_value=self.repo / "no-autopilot.toml"
        )
        self._config_patch.start()
        self.addCleanup(self._config_patch.stop)
        self._env_patch = mock.patch.dict(
            os.environ, {"OPSX_AUTOPILOT_NTFY_TOPIC": ""}, clear=False
        )
        self._env_patch.start()
        self.addCleanup(self._env_patch.stop)

    # -- helpers ----------------------------------------------------------
    def args(self, *, plan="plan.toml", once=False, veto=None, max_resets=None,
             spacing=None, poll=None) -> argparse.Namespace:
        return argparse.Namespace(
            repo=str(self.repo),
            plan=plan,
            once=once,
            veto_window_minutes=veto,
            max_auto_resets=max_resets,
            reset_spacing_seconds=spacing,
            poll_seconds=poll,
        )

    def run_autopilot(self, engine, *, clock=None, **argkw):
        clock = clock or FakeClock()
        rc = cmd_autopilot.cmd_autopilot(
            self.args(**argkw),
            runner=engine,
            now_func=clock,
            sleep_func=clock.sleep,
            executable=EXE,
        )
        return rc, clock

    def write_record(self, cid: str = CHANGE_ID, **fields) -> dict:
        state = state_mod.load_state(self.repo, PLAN_NAME)
        record = state_mod.rec(state, cid)
        record.update(fields)
        state_mod.save_state(self.repo, PLAN_NAME, state)
        return record

    def write_approval(self, cid: str = CHANGE_ID) -> None:
        state = state_mod.load_state(self.repo, PLAN_NAME)
        state.setdefault("approvals", [])
        if cid not in state["approvals"]:
            state["approvals"].append(cid)
        state_mod.save_state(self.repo, PLAN_NAME, state)

    @property
    def events(self) -> list[dict]:
        return read_jsonl(self.repo / ".opsx-plan" / "autopilot-events.jsonl")

    @property
    def escalations(self) -> list[dict]:
        return read_jsonl(self.repo / ".opsx-plan" / "escalations.jsonl")

    def event_names(self) -> list[str]:
        return [e.get("event") for e in self.events]

    def ap_state(self) -> dict:
        path = self.repo / ".opsx-plan" / "autopilot-state.json"
        return json.loads(path.read_text(encoding="utf-8")) if path.is_file() else {}


# ---------------------------------------------------------------------------
# Completion, continuation, budget
# ---------------------------------------------------------------------------
class CompletionAndBudgetTests(AutopilotHarness):
    def test_plan_complete_notifies_and_exits_zero(self) -> None:
        engine = FakeEngine([document(change(status="done"))])
        rc, _ = self.run_autopilot(engine)
        self.assertEqual(rc, 0)
        self.assertEqual(engine.run_count, 1)
        self.assertIn("plan_complete", self.event_names())

    def test_all_skipped_counts_as_complete(self) -> None:
        engine = FakeEngine([document(change(status="skipped"))])
        rc, _ = self.run_autopilot(engine)
        self.assertEqual(rc, 0)
        self.assertIn("plan_complete", self.event_names())

    def test_budget_pause_exits_zero_without_resetting(self) -> None:
        self.write_record(reason="budget exhausted while waiting to run archive")
        engine = FakeEngine([document(change(status="pending", reason="budget exhausted"))])
        rc, _ = self.run_autopilot(engine)
        self.assertEqual(rc, 0)
        self.assertEqual(engine.reset_count, 0)
        self.assertEqual(engine.approve_count, 0)
        self.assertEqual(self.escalations, [])

    def test_spend_budget_last_result_is_a_pause(self) -> None:
        self.write_record(last_result="spend_budget_exhausted", reason="")
        engine = FakeEngine([document(change(status="pending"))])
        rc, _ = self.run_autopilot(engine)
        self.assertEqual(rc, 0)
        self.assertEqual(engine.run_count, 1)

    def test_once_makes_a_single_pass(self) -> None:
        engine = FakeEngine([document(change(status="pending"))])
        rc, _ = self.run_autopilot(engine, once=True)
        self.assertEqual(rc, 0)
        self.assertEqual(engine.run_count, 1)


# ---------------------------------------------------------------------------
# Approval / veto window
# ---------------------------------------------------------------------------
class ApprovalWindowTests(AutopilotHarness):
    def test_window_expiry_invokes_approve_then_continues(self) -> None:
        engine = FakeEngine(
            [document(change(status="awaiting_approval", reason="pause_before")),
             document(change(status="done"))]
        )
        rc, _ = self.run_autopilot(
            engine, veto=1, poll=30
        )
        self.assertEqual(rc, 0)
        self.assertEqual(engine.approve_count, 1)
        approve_cmd = [c for c in engine.calls if c[1] == "approve"][0]
        self.assertIn(CHANGE_ID, approve_cmd)
        self.assertIn("approval_wait", self.event_names())
        self.assertIn("auto_approve", self.event_names())

    def test_out_of_band_approval_short_circuits_wait(self) -> None:
        self.write_approval()
        engine = FakeEngine([document(change(status="awaiting_approval"))])
        rc, _ = self.run_autopilot(engine, once=True, veto=60, poll=30)
        self.assertEqual(rc, 0)
        self.assertEqual(engine.approve_count, 0)
        self.assertNotIn("auto_approve", self.event_names())

    def test_veto_file_escalates_and_stops(self) -> None:
        veto_dir = self.repo / ".opsx-plan" / "veto"
        veto_dir.mkdir(parents=True)
        (veto_dir / CHANGE_ID).write_text("veto\n", encoding="utf-8")
        engine = FakeEngine([document(change(status="awaiting_approval"))])
        rc, _ = self.run_autopilot(engine, veto=60, poll=30)
        self.assertEqual(rc, 0)
        self.assertEqual(engine.approve_count, 0)
        self.assertIn("veto", self.event_names())
        classes = [e["class"] for e in self.escalations]
        self.assertEqual(classes, ["human_veto"])

    def test_approve_failure_escalates_environment_with_exit_two(self) -> None:
        engine = FakeEngine(
            [document(change(status="awaiting_approval"))], approve_rc=2
        )
        rc, _ = self.run_autopilot(engine, veto=1, poll=30)
        self.assertEqual(rc, 2)
        self.assertEqual([e["class"] for e in self.escalations], ["environment"])


# ---------------------------------------------------------------------------
# Failure classification
# ---------------------------------------------------------------------------
class FailureClassificationTests(AutopilotHarness):
    def test_permanent_provider_from_reason(self) -> None:
        self.write_record(reason="Insufficient Balance for provider account")
        engine = FakeEngine([document(change(status="failed"))], run_rc=1)
        rc, _ = self.run_autopilot(engine)
        self.assertEqual(rc, 0)
        self.assertEqual([e["class"] for e in self.escalations], ["permanent_provider"])

    def test_permanent_provider_from_log_tail(self) -> None:
        log = self.repo / "stage.log"
        log.write_text("worker output\nquota exceeded\n", encoding="utf-8")
        self.write_record(last_log=str(log), reason="")
        engine = FakeEngine([document(change(status="failed"))], run_rc=1)
        rc, _ = self.run_autopilot(engine)
        self.assertEqual(rc, 0)
        self.assertEqual([e["class"] for e in self.escalations], ["permanent_provider"])

    def test_permission_marker_escalates(self) -> None:
        self.write_record(reason="permission requested: external_directory")
        engine = FakeEngine([document(change(status="failed"))], run_rc=1)
        rc, _ = self.run_autopilot(engine)
        self.assertEqual(rc, 0)
        self.assertEqual([e["class"] for e in self.escalations], ["permission"])

    def test_needs_human_finding_recurrence(self) -> None:
        self.write_record(last_result="finding_recurrence_exceeded", reason="locus repeated")
        engine = FakeEngine([document(change(status="failed"))], run_rc=1)
        rc, _ = self.run_autopilot(engine)
        self.assertEqual(rc, 0)
        self.assertEqual(
            [e["class"] for e in self.escalations], ["finding_recurrence_exceeded"]
        )

    def test_needs_human_archive_status_failed(self) -> None:
        self.write_record(
            reason="", archive={"status": "failed", "reason": "delta missing"}
        )
        engine = FakeEngine([document(change(status="failed"))], run_rc=1)
        rc, _ = self.run_autopilot(engine)
        self.assertEqual(rc, 0)
        self.assertEqual([e["class"] for e in self.escalations], ["archive_failed"])

    def test_unknown_failure_escalates(self) -> None:
        self.write_record(reason="something odd happened", last_result="mystery")
        engine = FakeEngine([document(change(status="failed"))], run_rc=1)
        rc, _ = self.run_autopilot(engine)
        self.assertEqual(rc, 0)
        self.assertEqual([e["class"] for e in self.escalations], ["unknown"])

    def test_transient_timeout_schedules_reset(self) -> None:
        self.write_record(reason="implement timed out", last_result="implement_timeout")
        engine = FakeEngine(
            [document(change(status="failed")), document(change(status="done"))],
            run_rc=1,
        )
        rc, _ = self.run_autopilot(engine, max_resets=2, spacing=0)
        self.assertEqual(rc, 0)
        self.assertEqual(engine.reset_count, 1)
        self.assertIn("auto_reset", self.event_names())


# ---------------------------------------------------------------------------
# Bounded auto-reset
# ---------------------------------------------------------------------------
class AutoResetTests(AutopilotHarness):
    def _invalid(self) -> list[dict]:
        return [document(change(status="failed", reason="expected a final JSON object line"))]

    def test_auto_reset_then_recovery(self) -> None:
        self.write_record(last_result="subagent_output_invalid",
                          reason="expected a final JSON object line")
        engine = FakeEngine(
            [document(change(status="failed")), document(change(status="done"))],
            run_rc=1,
        )
        rc, _ = self.run_autopilot(engine, max_resets=2, spacing=0)
        self.assertEqual(rc, 0)
        self.assertEqual(engine.reset_count, 1)
        self.assertEqual(self.ap_state()["signatures"], {
            f"{CHANGE_ID}|subagent_output_invalid|expected a final JSON object line":
                {"count": 1, "last_attempt": "2026-01-02T03:04:05+00:00"}
        })

    def test_bounded_auto_reset_stops_at_max(self) -> None:
        self.write_record(last_result="subagent_output_invalid", reason="invalid JSON")
        engine = FakeEngine(self._invalid(), run_rc=1)
        rc, _ = self.run_autopilot(engine, max_resets=2, spacing=0)
        self.assertEqual(rc, 0)
        self.assertEqual(engine.reset_count, 2)
        self.assertEqual([e["class"] for e in self.escalations], ["transient_exhausted"])

    def test_reset_spacing_blocks_immediate_second_reset(self) -> None:
        self.write_record(last_result="subagent_output_invalid", reason="invalid JSON")
        engine = FakeEngine(self._invalid(), run_rc=1)
        rc, _ = self.run_autopilot(engine, max_resets=5, spacing=300)
        self.assertEqual(rc, 0)
        self.assertEqual(engine.reset_count, 1)
        self.assertEqual([e["class"] for e in self.escalations], ["transient_exhausted"])

    def test_signature_persists_across_state_reload(self) -> None:
        self.write_record(last_result="subagent_output_invalid", reason="invalid JSON")
        engine = FakeEngine(self._invalid(), run_rc=1)
        self.run_autopilot(engine, max_resets=1, spacing=0)
        path = self.repo / ".opsx-plan" / "autopilot-state.json"
        self.assertTrue(path.is_file())
        signatures = json.loads(path.read_text(encoding="utf-8"))["signatures"]
        self.assertEqual(len(signatures), 1)
        self.assertEqual(next(iter(signatures.values()))["count"], 1)

        # A fresh autopilot process reloads the persisted count and proceeds
        # to the next bounded attempt instead of resetting from zero.
        engine2 = FakeEngine(self._invalid(), run_rc=1)
        clock2 = FakeClock(start=datetime(2026, 1, 2, 4, 0, 0, tzinfo=timezone.utc))
        cmd_autopilot.cmd_autopilot(
            self.args(max_resets=2, spacing=0),
            runner=engine2,
            now_func=clock2,
            sleep_func=clock2.sleep,
            executable=EXE,
        )
        self.assertEqual(engine2.reset_count, 1)
        persisted = json.loads(path.read_text(encoding="utf-8"))["signatures"]
        self.assertEqual(next(iter(persisted.values()))["count"], 2)


# ---------------------------------------------------------------------------
# Escalation evidence, dedup, ntfy
# ---------------------------------------------------------------------------
class EscalationTests(AutopilotHarness):
    def test_escalation_jsonl_record_and_digest_fields(self) -> None:
        log = self.repo / "logs" / "implement.r1.log"
        log.parent.mkdir(parents=True)
        log.write_text("billing hard limit reached\n", encoding="utf-8")
        self.write_record(
            reason="billing hard limit", last_result="implement_invalid",
            attempts=3, last_log=str(log),
            history=[{
                "phase": "review", "round": 1,
                "findings": [
                    {"severity": "critical", "locus": ["src/a.py"]},
                    {"severity": "warning", "locus": ["src/b.py"]},
                ],
            }],
        )
        engine = FakeEngine([document(change(status="failed"))], run_rc=1)
        rc, _ = self.run_autopilot(engine)
        self.assertEqual(rc, 0)
        self.assertEqual(len(self.escalations), 1)
        entry = self.escalations[0]
        self.assertEqual(entry["plan"], PLAN_NAME)
        self.assertEqual(entry["change_id"], CHANGE_ID)
        self.assertEqual(entry["class"], "permanent_provider")
        self.assertEqual(entry["attempts"], 3)
        self.assertEqual(entry["last_result"], "implement_invalid")
        self.assertEqual(entry["loci"], ["src/a.py", "src/b.py"])
        self.assertIn("balance", entry["suggested_action"])
        self.assertIn("escalate", self.event_names())

    def test_notified_dedup_same_day(self) -> None:
        self.write_record(reason="quota exceeded")
        engine1 = FakeEngine([document(change(status="failed"))], run_rc=1)
        with mock.patch.dict(os.environ, {"OPSX_AUTOPILOT_NTFY_TOPIC": "topic"}), \
                mock.patch.object(cmd_autopilot.urllib.request, "urlopen") as urlopen:
            self.run_autopilot(engine1)
        self.assertEqual(urlopen.call_count, 1)

        engine2 = FakeEngine([document(change(status="failed"))], run_rc=1)
        with mock.patch.dict(os.environ, {"OPSX_AUTOPILOT_NTFY_TOPIC": "topic"}), \
                mock.patch.object(cmd_autopilot.urllib.request, "urlopen") as urlopen2:
            self.run_autopilot(engine2)
        self.assertEqual(urlopen2.call_count, 0)
        self.assertEqual(len(self.escalations), 2)

    def test_ntfy_skipped_without_topic(self) -> None:
        self.write_record(reason="quota exceeded")
        engine = FakeEngine([document(change(status="failed"))], run_rc=1)
        with mock.patch.object(cmd_autopilot.urllib.request, "urlopen") as urlopen:
            rc, _ = self.run_autopilot(engine)
        self.assertEqual(rc, 0)
        urlopen.assert_not_called()
        self.assertEqual(len(self.escalations), 1)
        self.assertNotIn("notify_failed", self.event_names())

    def test_ntfy_exception_is_swallowed(self) -> None:
        self.write_record(reason="quota exceeded")
        engine = FakeEngine([document(change(status="failed"))], run_rc=1)
        with mock.patch.dict(os.environ, {"OPSX_AUTOPILOT_NTFY_TOPIC": "topic"}), \
                mock.patch.object(
                    cmd_autopilot.urllib.request, "urlopen",
                    side_effect=OSError("network down"),
                ):
            rc, _ = self.run_autopilot(engine)
        self.assertEqual(rc, 0)
        self.assertEqual(len(self.escalations), 1)
        self.assertIn("notify_failed", self.event_names())


# ---------------------------------------------------------------------------
# No-progress guard and environment
# ---------------------------------------------------------------------------
class NoProgressAndEnvironmentTests(AutopilotHarness):
    def test_no_progress_guard_fires_after_three_identical_passes(self) -> None:
        engine = FakeEngine([document(change(status="ready"))])
        rc, _ = self.run_autopilot(engine)
        self.assertEqual(rc, 0)
        self.assertEqual(engine.run_count, 3)
        self.assertIn("no_progress_guard", self.event_names())
        self.assertEqual([e["class"] for e in self.escalations], ["no_forward_progress"])

    def test_environment_escalation_exits_two(self) -> None:
        engine = FakeEngine([document(change(status="pending"))], run_rc=2)
        rc, _ = self.run_autopilot(engine)
        self.assertEqual(rc, 2)
        self.assertEqual([e["class"] for e in self.escalations], ["environment"])

    def test_invalid_status_json_is_environment(self) -> None:
        engine = FakeEngine([], status_raw="not json")
        rc, _ = self.run_autopilot(engine)
        self.assertEqual(rc, 2)
        self.assertEqual([e["class"] for e in self.escalations], ["environment"])


# ---------------------------------------------------------------------------
# Preflight
# ---------------------------------------------------------------------------
class PreflightTests(AutopilotHarness):
    def test_dirty_tracked_tree_exits_two_before_running(self) -> None:
        (self.repo / "tracked.txt").write_text("dirty\n", encoding="utf-8")
        engine = FakeEngine([document(change(status="done"))])
        rc, _ = self.run_autopilot(engine)
        self.assertEqual(rc, 2)
        self.assertEqual(engine.run_count, 0)

    def test_unresolvable_executable_exits_two(self) -> None:
        engine = FakeEngine([document(change(status="done"))])
        with mock.patch.object(cmd_autopilot, "_resolve_opsx_plan", return_value=None):
            rc = cmd_autopilot.cmd_autopilot(
                self.args(), runner=engine, now_func=FakeClock(),
                sleep_func=FakeClock().sleep, executable=None,
            )
        self.assertEqual(rc, 2)
        self.assertEqual(engine.run_count, 0)

    def test_missing_plan_exits_two(self) -> None:
        engine = FakeEngine([document(change(status="done"))])
        rc, _ = self.run_autopilot(engine, plan="no-such-plan.toml")
        self.assertEqual(rc, 2)
        self.assertEqual(engine.run_count, 0)


if __name__ == "__main__":
    unittest.main()
