"""Tests for ``lib.orchestrator.cmd_autopilot``.

The engine is faked through the injectable *runner*; the clock is faked
through *now_func*/*sleep_func*.  Every test uses a throwaway git repo and a
minimal plan TOML so plan resolution exercises the real
``planref.resolve_plan``/``load_plan`` path.
"""

from __future__ import annotations

import argparse
import io
import json
import os
import subprocess
import sys
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from contextlib import redirect_stdout, redirect_stderr
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
        run_stdout: str = "",
        run_stderr: str = "",
    ) -> None:
        self.status_docs = list(status_docs)
        self.run_rc = run_rc
        self.approve_rc = approve_rc
        self.reset_rc = reset_rc
        self.status_rc = status_rc
        self.status_raw = status_raw
        self.run_stdout = run_stdout
        self.run_stderr = run_stderr
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
            return result(self.run_rc, self.run_stdout, self.run_stderr)
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

    @property
    def pause_path(self) -> Path:
        return self.repo / ".opsx-plan" / "autopilot-paused.json"


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

    def test_resolution_banner_before_json_is_tolerated(self) -> None:
        banner = "[opsx-plan 00:00:00] using plan from OPSX_PLAN: plan.toml\n"
        engine = FakeEngine(
            [], status_raw=banner + json.dumps(document(change(status="done")))
        )
        rc, _ = self.run_autopilot(engine)
        self.assertEqual(rc, 0)
        self.assertIn("plan_complete", self.event_names())
        self.assertEqual(self.escalations, [])


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
        self.assertFalse(self.pause_path.exists())

    def test_lock_contention_stays_transient(self) -> None:
        reason = "worktree /repo execution lock is already held by pid 123; refusing to proceed"
        engine = FakeEngine(
            [document(change(status="done"))], run_rc=2, run_stderr=reason,
        )
        rc, _ = self.run_autopilot(engine)
        self.assertEqual(rc, 2)
        self.assertFalse(self.pause_path.exists())
        self.assertEqual(len(self.escalations), 1)
        self.assertEqual(self.escalations[0]["reason"], reason)

    def test_deterministic_engine_refusals_pause_before_status(self) -> None:
        for reason in (
            "[opsx-plan] tracked worktree is dirty; refusing to start a new stage",
            "error: uncommitted archive changes; operator must commit them",
            "error: cannot parse plan plan.toml: file missing",
            "error: cannot locate the opsx-plan executable",
        ):
            with self.subTest(reason=reason):
                cmd_autopilot._clear_pause(self.repo)
                before = len(self.escalations)
                engine = FakeEngine(
                    [document(change(status="done"))], run_rc=2,
                    run_stdout="engine banner\n", run_stderr=reason,
                )
                rc, _ = self.run_autopilot(engine)
                self.assertEqual(rc, 0)
                self.assertEqual(len(engine.calls), 1)
                self.assertTrue(self.pause_path.exists())
                self.assertEqual(self.escalations[-1]["reason"], reason)
                self.assertEqual(len(self.escalations), before + 1)

    def test_unknown_engine_diagnostic_stays_transient(self) -> None:
        reason = "error: temporary service unavailable"
        engine = FakeEngine([], run_rc=2, run_stderr=reason)
        rc, _ = self.run_autopilot(engine)
        self.assertEqual(rc, 2)
        self.assertFalse(self.pause_path.exists())
        self.assertEqual(self.escalations[0]["reason"], reason)

    def test_real_engine_stream_is_captured_for_pause(self) -> None:
        # Exercise the actual runner, not only caller-supplied diagnostics.
        exe = self.repo / "fake-opsx-plan"
        exe.write_text(
            f"#!{sys.executable}\nimport sys\n"
            "print('engine output', flush=True)\n"
            "print('tracked worktree is dirty; refusing to start a new stage', file=sys.stderr)\n"
            "sys.exit(2)\n",
            encoding="utf-8",
        )
        exe.chmod(0o755)
        out = io.StringIO()
        with redirect_stdout(out):
            rc = cmd_autopilot.cmd_autopilot(self.args(), executable=str(exe))
        self.assertEqual(rc, 0)
        self.assertIn("engine output", out.getvalue())
        self.assertIn("tracked worktree is dirty", out.getvalue())
        self.assertEqual(len(self.escalations), 1)
        self.assertTrue(self.pause_path.exists())


# ---------------------------------------------------------------------------
# Acceptance of orchestrator-created changes
# ---------------------------------------------------------------------------
class AcceptanceWaitTests(AutopilotHarness):
    def test_acceptance_waits_until_operator_accepts(self) -> None:
        engine = FakeEngine(
            [document(change(status="awaiting_acceptance", reason="created and verified")),
             document(change(status="done"))]
        )
        harness = self

        class AcceptOnFirstSleep(FakeClock):
            def sleep(self, seconds: float) -> None:
                super().sleep(seconds)
                harness.write_record(accepted=True)

        rc, _ = self.run_autopilot(engine, clock=AcceptOnFirstSleep(), poll=30)
        self.assertEqual(rc, 0)
        self.assertEqual(engine.run_count, 2)
        self.assertIn("acceptance_wait", self.event_names())
        self.assertIn("accepted", self.event_names())
        self.assertIn("plan_complete", self.event_names())
        self.assertEqual(self.escalations, [])
        self.assertEqual(engine.approve_count, 0)

    def test_acceptance_once_announces_and_exits(self) -> None:
        engine = FakeEngine(
            [document(change(status="awaiting_acceptance", reason="created and verified"))]
        )
        rc, _ = self.run_autopilot(engine, once=True, poll=30)
        self.assertEqual(rc, 0)
        self.assertEqual(engine.run_count, 1)
        self.assertIn("acceptance_wait", self.event_names())
        self.assertNotIn("accepted", self.event_names())
        self.assertEqual(self.escalations, [])

    def test_acceptance_already_accepted_does_not_block(self) -> None:
        self.write_record(accepted=True)
        engine = FakeEngine(
            [document(change(status="awaiting_acceptance")),
             document(change(status="done"))]
        )
        rc, _ = self.run_autopilot(engine, poll=30)
        self.assertEqual(rc, 0)
        self.assertIn("accepted", self.event_names())
        self.assertIn("plan_complete", self.event_names())


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

    def test_status_trailing_text_after_json_is_tolerated(self) -> None:
        engine = FakeEngine(
            [],
            status_raw=(
                json.dumps(document(change(status="done")))
                + "\n[opsx-plan 00:00:00] trailing notice\n"
            ),
        )
        rc, _ = self.run_autopilot(engine)
        self.assertEqual(rc, 0)
        self.assertIn("plan_complete", self.event_names())


# ---------------------------------------------------------------------------
# Preflight
# ---------------------------------------------------------------------------
class PreflightTests(AutopilotHarness):
    def test_dirty_tracked_tree_pauses_once_before_running(self) -> None:
        (self.repo / "tracked.txt").write_text("dirty\n", encoding="utf-8")
        engine = FakeEngine([document(change(status="done"))])
        with mock.patch.dict(os.environ, {"OPSX_AUTOPILOT_NTFY_TOPIC": "topic"}), \
                mock.patch.object(cmd_autopilot.urllib.request, "urlopen") as urlopen:
            rc, _ = self.run_autopilot(engine)
            marker_bytes = self.pause_path.read_bytes()
            # Even an explicit second start with a broken plan must no-op.
            with mock.patch.object(cmd_autopilot.planref, "resolve_plan") as resolve:
                second_rc, _ = self.run_autopilot(engine, plan="missing.toml")
                once_rc, _ = self.run_autopilot(engine, once=True)
            resolve.assert_not_called()
        self.assertEqual((rc, second_rc, once_rc), (0, 0, 0))
        self.assertEqual(engine.run_count, 0)
        self.assertEqual(urlopen.call_count, 1)
        self.assertEqual(len(self.escalations), 1)
        self.assertEqual(self.event_names().count("escalate"), 1)
        self.assertEqual(self.event_names().count("paused"), 2)
        self.assertEqual(self.pause_path.read_bytes(), marker_bytes)
        marker = json.loads(marker_bytes)
        self.assertEqual(marker["class"], "deterministic")
        self.assertIn("dirty", marker["reason"])
        self.assertEqual(marker["created_at"], FakeClock()().isoformat())
        self.assertIn("resume", marker["suggested_action"])

    def test_unresolvable_executable_pauses(self) -> None:
        engine = FakeEngine([document(change(status="done"))])
        with mock.patch.object(cmd_autopilot, "_resolve_opsx_plan", return_value=None):
            rc = cmd_autopilot.cmd_autopilot(
                self.args(), runner=engine, now_func=FakeClock(),
                sleep_func=FakeClock().sleep, executable=None,
            )
        self.assertEqual(rc, 0)
        self.assertEqual(engine.run_count, 0)
        self.assertTrue(self.pause_path.exists())
        self.assertEqual(len(self.escalations), 1)
        self.assertIn("executable", self.escalations[0]["reason"])

    def test_missing_plan_pauses(self) -> None:
        engine = FakeEngine([document(change(status="done"))])
        rc, _ = self.run_autopilot(engine, plan="no-such-plan.toml")
        self.assertEqual(rc, 0)
        self.assertEqual(engine.run_count, 0)
        self.assertTrue(self.pause_path.exists())
        self.assertEqual(len(self.escalations), 1)

    def test_malformed_plan_pauses_and_second_start_noops(self) -> None:
        self.plan.write_text("not valid TOML [", encoding="utf-8")
        engine = FakeEngine([])
        self.assertEqual(self.run_autopilot(engine)[0], 0)
        self.assertTrue(self.pause_path.exists())
        self.assertEqual(self.run_autopilot(engine, once=True)[0], 0)
        self.assertEqual(len(self.escalations), 1)
        self.assertEqual(engine.calls, [])

    def test_marker_is_atomic_and_does_not_dirty_tracked_tree(self) -> None:
        marker = {"class": "deterministic", "reason": "test", "created_at": "now", "suggested_action": "resume"}
        with mock.patch.object(cmd_autopilot.os, "fsync", wraps=os.fsync) as fsync, \
                mock.patch.object(cmd_autopilot.os, "replace", wraps=os.replace) as replace:
            cmd_autopilot._write_pause(self.repo, marker)
        fsync.assert_called_once()
        replace.assert_called_once()
        self.assertEqual(cmd_autopilot._load_pause(self.repo), marker)
        self.assertFalse(self.pause_path.with_suffix(".tmp").exists())
        self.assertTrue(cmd_autopilot.groundtruth.tracked_tree_clean(self.repo))
        cmd_autopilot._clear_pause(self.repo)
        self.assertIsNone(cmd_autopilot._load_pause(self.repo))

    def test_malformed_marker_still_parks_run(self) -> None:
        self.pause_path.parent.mkdir()
        self.pause_path.write_text("not json", encoding="utf-8")
        engine = FakeEngine([])
        with mock.patch.object(cmd_autopilot.planref, "resolve_plan") as resolve:
            rc, _ = self.run_autopilot(engine, once=True)
        self.assertEqual(rc, 0)
        resolve.assert_not_called()
        self.assertEqual(self.escalations, [])
        self.assertEqual(self.event_names(), ["paused"])

    def test_unclassified_preflight_environment_error_retries(self) -> None:
        engine = FakeEngine([])
        with mock.patch.object(cmd_autopilot.groundtruth, "tracked_tree_clean", side_effect=OSError("git unavailable")):
            rc, _ = self.run_autopilot(engine)
        self.assertEqual(rc, 2)
        self.assertFalse(self.pause_path.exists())
        self.assertEqual(len(self.escalations), 1)
        self.assertIn("git unavailable", self.escalations[0]["reason"])
        self.assertEqual(engine.calls, [])


class StatusAndResumeTests(AutopilotHarness):
    def pause(self) -> None:
        cmd_autopilot._write_pause(self.repo, {
            "class": "deterministic", "reason": "tracked worktree is dirty",
            "created_at": "2026-01-02T03:04:05Z", "suggested_action": "fix and resume",
        })

    def snapshot(self) -> dict:
        return {
            str(path.relative_to(self.repo)): path.read_bytes()
            for path in (self.repo / ".opsx-plan").rglob("*") if path.is_file()
        }

    def test_status_is_read_only_and_shows_pause_and_recorded_state(self) -> None:
        self.pause()
        self.write_record(status="failed", reason="test failure")
        before = self.snapshot()
        out = io.StringIO()
        with redirect_stdout(out):
            rc = cmd_autopilot.cmd_autopilot_status(self.args())
        self.assertEqual(rc, 0)
        for text in ("autopilot: paused", "class: deterministic", "tracked worktree is dirty",
                     "2026-01-02T03:04:05Z", "suggested_action: fix and resume",
                     f"plan: {PLAN_NAME}", f"{CHANGE_ID}: failed", "test failure"):
            self.assertIn(text, out.getvalue())
        self.assertEqual(self.snapshot(), before)

    def test_status_reports_pause_even_when_plan_is_malformed(self) -> None:
        self.pause()
        self.plan.write_text("[bad TOML", encoding="utf-8")
        before = self.snapshot()
        out = io.StringIO()
        with redirect_stdout(out):
            rc = cmd_autopilot.cmd_autopilot_status(self.args())
        self.assertEqual(rc, 0)
        self.assertIn("autopilot: paused", out.getvalue())
        self.assertIn("plan status unavailable:", out.getvalue())
        self.assertEqual(self.snapshot(), before)

    def test_status_without_pause_does_not_create_state(self) -> None:
        out = io.StringIO()
        with redirect_stdout(out):
            rc = cmd_autopilot.cmd_autopilot_status(self.args())
        self.assertEqual(rc, 0)
        self.assertIn("autopilot: not paused", out.getvalue())
        self.assertIn(f"{CHANGE_ID}: pending", out.getvalue())
        self.assertFalse((self.repo / ".opsx-plan").exists())

    def test_resume_refuses_dirty_then_clears_clean_without_engine_run(self) -> None:
        self.pause()
        (self.repo / "tracked.txt").write_text("dirty\n", encoding="utf-8")
        before = self.snapshot()
        err = io.StringIO()
        with redirect_stderr(err):
            rc = cmd_autopilot.cmd_autopilot_resume(self.args(), executable=EXE)
        self.assertEqual(rc, 2)
        self.assertIn("dirty", err.getvalue())
        self.assertEqual(self.snapshot(), before)
        git(self.repo, "checkout", "--", "tracked.txt")
        with mock.patch.object(cmd_autopilot, "_subprocess_runner") as runner:
            rc = cmd_autopilot.cmd_autopilot_resume(self.args(), executable=EXE)
        self.assertEqual(rc, 0)
        self.assertFalse(self.pause_path.exists())
        self.assertEqual(self.event_names(), ["resumed"])
        runner.assert_not_called()
        self.assertEqual(self.escalations, [])

    def test_resume_keeps_marker_when_plan_or_executable_unresolvable(self) -> None:
        self.pause()
        before = self.snapshot()
        with redirect_stderr(io.StringIO()):
            rc = cmd_autopilot.cmd_autopilot_resume(self.args(plan="missing.toml"), executable=EXE)
        self.assertEqual(rc, 2)
        self.assertEqual(self.snapshot(), before)
        with mock.patch.object(cmd_autopilot, "_resolve_opsx_plan", return_value=None), \
                redirect_stderr(io.StringIO()):
            rc = cmd_autopilot.cmd_autopilot_resume(self.args())
        self.assertEqual(rc, 2)
        self.assertEqual(self.snapshot(), before)

    def test_resume_respects_clean_tree_opt_out(self) -> None:
        self.pause()
        self.plan.write_text(self.plan.read_text().replace(
            '[plan]\n', '[plan]\nrequire_clean_tracked = false\n'
        ), encoding="utf-8")
        (self.repo / "tracked.txt").write_text("dirty\n", encoding="utf-8")
        self.assertEqual(cmd_autopilot.cmd_autopilot_resume(self.args(), executable=EXE), 0)
        self.assertFalse(self.pause_path.exists())

    def test_cli_parser_and_dispatch_for_status_resume_and_paused_once(self) -> None:
        self.pause()
        script = Path(__file__).resolve().parents[2] / "orchestrator" / "opsx-plan.py"
        command = [sys.executable, str(script), "--repo", str(self.repo), "autopilot"]
        status = subprocess.run(
            [*command, "status", "--plan", "plan.toml"], capture_output=True, text=True,
        )
        self.assertEqual(status.returncode, 0, status.stderr)
        self.assertIn("autopilot: paused", status.stdout)
        once = subprocess.run(
            [*command, "--once", "--plan", "missing.toml"], capture_output=True, text=True,
        )
        self.assertEqual(once.returncode, 0, once.stderr)
        self.assertTrue(self.pause_path.exists())
        with mock.patch.dict(os.environ, {"PATH": ""}):
            refused = subprocess.run(
                [*command, "resume", "--plan", "plan.toml"], capture_output=True, text=True,
            )
        self.assertEqual(refused.returncode, 2, refused.stderr)
        self.assertTrue(self.pause_path.exists())
        # Supply a resolvable engine, but resume must never invoke it.
        exe = self.repo / "opsx-plan"
        exe.write_text("#!/bin/sh\nexit 99\n", encoding="utf-8")
        exe.chmod(0o755)
        with mock.patch.dict(os.environ, {"PATH": str(self.repo) + os.pathsep + os.environ.get("PATH", "")}):
            resumed = subprocess.run(
                [*command, "resume", "--plan", "plan.toml"], capture_output=True, text=True,
            )
        self.assertEqual(resumed.returncode, 0, resumed.stderr)
        self.assertFalse(self.pause_path.exists())


if __name__ == "__main__":
    unittest.main()
