"""Tests for ``add-watchdog-reconstitution``.

Covers the durable reconstitution-event ledger, the three independent
watchdog signals, the classification vocabulary with its precedence, boot-scan
reconciliation (reconnect before respawn), quiescence-gated reconstitution,
bounded persisted restart backoff, unreconciled uncertain-action blocking, the
no-action rule for an expected human wait, the append-only event discipline,
and the ``opsx-plan supervise watchdog`` CLI surface.

The tests use a temp repository and trusted ledger storage, real local
subprocesses for live/dead fencing identities, a fake clock, and injected
window constants. No external network or paid model is contacted.
"""

from __future__ import annotations

import argparse
import contextlib
import fcntl
import http.server
import io
import json
import os
import subprocess
import sys
import tempfile
import threading
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest import mock

from lib.orchestrator import cmd_supervise
from lib.supervisor import clock, ledger, lock, session_bridge, watchdog

UTC = timezone.utc


def _policy(**overrides: object) -> dict:
    base = {
        "authority_config": {"mode": "policy-bound", "approval": "supervisor"},
        "model_selection": {
            "version": 1,
            "roles": {"implementer": "cheap/model-a"},
            "stages": {"implement": "implementer"},
        },
        "inexpensive_allowlist": {
            "version": 1,
            "models": ["cheap/model-a", "cheap/model-b"],
            "source": "test fixture",
        },
        "manifest_snapshot_hash": "deadbeef",
        "budgets": {
            "version": 1,
            "total_cost_usd": 100.0,
            "per_action_cost_usd": None,
            "total_elapsed_minutes": None,
            "per_action_elapsed_minutes": None,
            "max_incident_attempts": 3,
        },
        "deadlines": {"version": 1, "execution_deadline_minutes": None},
    }
    base.update(overrides)
    return base


_MANIFEST = (
    "[plan]\n"
    'name = "watchdog-test"\n'
    'adapter = "opencode"\n\n'
    "[[changes]]\n"
    'id = "change-a"\n'
    "phase = 1\n"
)


def _iso(dt: datetime) -> str:
    return dt.astimezone(UTC).isoformat(timespec="seconds")


class WatchdogTestCase(unittest.TestCase):
    """Shared temp layout: a repository and trusted ledger storage."""

    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.root = Path(self._tmp.name)
        self.repo = self.root / "repo"
        self.repo.mkdir()
        (self.repo / ".git").mkdir()
        self.worktree = self.repo
        self.storage = self.root / "service-storage"
        self.storage.mkdir()
        self.db_path = self.storage / "supervisor.sqlite3"
        self.ledger = self._open()
        self._processes: list[subprocess.Popen] = []
        self.addCleanup(self._reap)
        env = mock.patch.dict(
            os.environ, {"OPSX_SUPERVISOR_STATE_FILE": str(self.db_path)}
        )
        env.start()
        self.addCleanup(env.stop)

    def _reap(self) -> None:
        for process in self._processes:
            try:
                process.kill()
            except Exception:  # noqa: BLE001 - best-effort cleanup
                pass
            try:
                process.wait(timeout=5)
            except Exception:  # noqa: BLE001 - best-effort cleanup
                pass

    def _open(self) -> ledger.Ledger:
        handle = ledger.open_ledger(self.db_path, repository_root=self.repo)
        self.addCleanup(handle.close)
        return handle

    def register(
        self,
        *,
        content: str = _MANIFEST,
        state: str = "registered",
        policy: dict | None = None,
        worktree: Path | None = None,
    ) -> int:
        job_id = self.ledger.register_job(
            run_id="run-watchdog",
            worktree=worktree if worktree is not None else self.worktree,
            owner="service",
            operator="operator",
            policy=policy or _policy(),
            manifest_content=content,
        )
        if state != "registered":
            self.ledger.set_job_state(job_id, state)
        return job_id

    def spawn_live_process(self) -> subprocess.Popen:
        process = subprocess.Popen(
            [sys.executable, "-c", "import time; time.sleep(60)"]
        )
        self._processes.append(process)
        # Give the OS a moment to publish the process start time.
        for _ in range(50):
            if lock.process_start_time(process.pid) is not None:
                break
            import time as _time

            _time.sleep(0.02)
        return process

    def record_live_owner(self, job_id: int) -> subprocess.Popen:
        process = self.spawn_live_process()
        self.ledger.record_fencing(
            job_id,
            event="acquired",
            owner="opsx-plan supervise test",
            pid=process.pid,
            process_start=lock.process_start_time(process.pid),
            boot_id=lock.boot_identity(),
            host=None,
        )
        return process

    def record_dead_owner(self, job_id: int) -> int:
        process = self.spawn_live_process()
        start = lock.process_start_time(process.pid)
        process.kill()
        process.wait(timeout=10)
        return self.ledger.record_fencing(
            job_id,
            event="released",
            owner="opsx-plan supervise test",
            pid=process.pid,
            process_start=start,
            boot_id=lock.boot_identity(),
            host=None,
        )

    def runner(self, **kwargs: object) -> watchdog.Watchdog:
        params: dict = {"repo": self.repo}
        params.update(kwargs)
        return watchdog.Watchdog(self.ledger, **params)

    def job(self, job_id: int):
        return self.ledger.get_job(job_id)


class SignalAndClassificationTests(WatchdogTestCase):
    def test_liveness_and_progress_are_reported_separately(self) -> None:
        job_id = self.register()
        self.record_live_owner(job_id)
        signals = self.runner().signals_for(
            self.job(job_id),
            now=_iso(_now_after(self.job(job_id)["created_at"], 3600)),
        )
        self.assertTrue(signals.liveness)
        self.assertFalse(signals.progress)
        self.assertEqual(watchdog.classify(signals), watchdog.CLASS_STALLED)

    def test_progress_is_reported_without_liveness(self) -> None:
        job_id = self.register()
        self.record_dead_owner(job_id)
        signals = self.runner().signals_for(
            self.job(job_id),
            now=_iso(_now_after(self.job(job_id)["created_at"], 1)),
        )
        self.assertFalse(signals.liveness)
        self.assertTrue(signals.progress)

    def test_deadline_signal_excludes_human_wait(self) -> None:
        policy = _policy(deadlines={"version": 1, "execution_deadline_minutes": 0.0001})
        job_id = self.register(policy=policy)
        self.record_dead_owner(job_id)
        self.ledger.record_wait(
            job_id,
            kind="human",
            checkpoint="approval",
            material_hash="hash-1",
            change_id="change-a",
        )
        signals = self.runner().signals_for(
            self.job(job_id),
            now=_iso(_now_after(self.job(job_id)["created_at"], 7200)),
        )
        # No dispatch interval accrues execution elapsed, so the long human
        # wait does not trip the deadline.
        self.assertEqual(signals.execution_elapsed_minutes, 0.0)
        self.assertFalse(signals.deadline)

    def test_expected_human_wait_takes_precedence_over_dead(self) -> None:
        job_id = self.register()
        self.record_dead_owner(job_id)
        self.ledger.record_wait(
            job_id,
            kind="human",
            checkpoint="approval",
            material_hash="hash-1",
            change_id="change-a",
        )
        signals = self.runner().signals_for(self.job(job_id))
        self.assertTrue(signals.expected_human_wait)
        self.assertEqual(
            watchdog.classify(signals), watchdog.CLASS_EXPECTED_HUMAN_WAIT
        )

    def test_live_job_making_progress_is_live(self) -> None:
        job_id = self.register()
        self.record_live_owner(job_id)
        signals = self.runner().signals_for(
            self.job(job_id),
            now=_iso(_now_after(self.job(job_id)["created_at"], 1)),
        )
        self.assertEqual(watchdog.classify(signals), watchdog.CLASS_LIVE)

    def test_live_job_without_recent_progress_is_quiet(self) -> None:
        job_id = self.register()
        self.record_live_owner(job_id)
        runner = self.runner(progress_window_seconds=10.0, stall_threshold_seconds=100.0)
        signals = runner.signals_for(
            self.job(job_id),
            now=_iso(_now_after(self.job(job_id)["created_at"], 50)),
        )
        self.assertTrue(signals.liveness)
        self.assertFalse(signals.progress)
        self.assertEqual(watchdog.classify(signals), watchdog.CLASS_QUIET)

    def test_no_progress_beyond_stall_is_stalled(self) -> None:
        job_id = self.register()
        self.record_live_owner(job_id)
        runner = self.runner(progress_window_seconds=10.0, stall_threshold_seconds=100.0)
        signals = runner.signals_for(
            self.job(job_id),
            now=_iso(_now_after(self.job(job_id)["created_at"], 500)),
        )
        self.assertEqual(watchdog.classify(signals), watchdog.CLASS_STALLED)

    def test_deadline_reached_is_stalled(self) -> None:
        policy = _policy(deadlines={"version": 1, "execution_deadline_minutes": 0.0})
        job_id = self.register(policy=policy)
        self.record_dead_owner(job_id)
        signals = self.runner().signals_for(self.job(job_id))
        self.assertTrue(signals.deadline)
        self.assertEqual(watchdog.classify(signals), watchdog.CLASS_STALLED)

    def test_quiesced_non_live_owner_is_dead(self) -> None:
        job_id = self.register()
        self.record_dead_owner(job_id)
        signals = self.runner().signals_for(self.job(job_id))
        self.assertFalse(signals.liveness)
        self.assertTrue(signals.quiesced)
        self.assertEqual(watchdog.classify(signals), watchdog.CLASS_DEAD)

    def test_live_owner_is_not_quiesced_even_with_a_free_lock(self) -> None:
        job_id = self.register()
        self.record_live_owner(job_id)
        owner = watchdog.recorded_owner(self.ledger, job_id)
        self.assertIsNotNone(owner)
        # The kernel-held flock is free (no lock file) yet the recorded
        # identity still matches a live process: quiescence is not verified.
        self.assertFalse(watchdog.verify_quiescence(self.repo, owner))

    def test_held_kernel_lock_blocks_quiescence(self) -> None:
        job_id = self.register()
        self.record_dead_owner(job_id)
        owner = watchdog.recorded_owner(self.ledger, job_id)
        lock_path = lock.lock_file_path(self.repo)
        lock_path.parent.mkdir(parents=True, exist_ok=True)
        fd = os.open(str(lock_path), os.O_RDWR | os.O_CREAT, 0o644)
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
            self.assertFalse(watchdog.verify_quiescence(self.repo, owner))
        finally:
            fcntl.flock(fd, fcntl.LOCK_UN)
            os.close(fd)
        self.assertTrue(watchdog.verify_quiescence(self.repo, owner))

    def test_classification_is_reproducible_from_durable_state(self) -> None:
        job_id = self.register()
        self.record_dead_owner(job_id)
        runner = self.runner()
        first = runner.report(self.job(job_id))
        second = runner.report(self.job(job_id))
        self.assertEqual(first["classification"], second["classification"])
        self.assertEqual(first["action"], second["action"])
        # A read-only report never mutates the ledger's attempt state.
        self.assertEqual(first["restart"]["attempts"], 0)
        self.assertEqual(second["restart"]["attempts"], 0)


class BootScanAndDecisionTests(WatchdogTestCase):
    def test_boot_scan_records_each_non_terminal_job_and_skips_terminal(self) -> None:
        other_worktree = self.repo / "other-worktree"
        other_worktree.mkdir()
        terminal = self.register(worktree=other_worktree)
        self.ledger.set_job_state(terminal, "completed")
        active = self.register()
        runner = self.runner()
        assessments = runner.boot_scan()
        self.assertEqual([item.job_id for item in assessments], [active])
        self.assertEqual(
            len(self.ledger.list_watchdog_events(active, limit=None)), 1
        )
        self.assertEqual(self.ledger.list_watchdog_events(terminal), [])

    def test_live_session_is_adopted_and_not_respawned(self) -> None:
        job_id = self.register()
        self.record_dead_owner(job_id)
        session_bridge.record_primary_session_linkage(
            self.ledger,
            job_id,
            run_id="run-watchdog",
            server_address="http://127.0.0.1:1",
            session_id="session-1",
        )
        redrive_calls: list[int] = []
        runner = self.runner(
            reconnect=lambda _ledger, _job_id: True,
            redrive=lambda _ledger, jid: redrive_calls.append(jid),
        )
        assessments = runner.boot_scan()
        assessment = assessments[0]
        self.assertEqual(assessment.action, watchdog.ACTION_RECONNECT)
        self.assertEqual(redrive_calls, [])
        kinds = [str(row["kind"]) for row in self.ledger.list_watchdog_events(job_id)]
        self.assertIn(watchdog.EVENT_RECONNECTED, kinds)

    def test_live_owner_is_surfaced_never_interrupted(self) -> None:
        job_id = self.register()
        self.record_live_owner(job_id)
        redrive_calls: list[int] = []
        runner = self.runner(
            progress_window_seconds=10.0,
            stall_threshold_seconds=100.0,
            redrive=lambda _ledger, jid: redrive_calls.append(jid),
        )
        assessment = runner.assess(
            self.job(job_id),
            record=True,
        )
        # A live owner is never reconstituted, however long it has been quiet.
        self.assertEqual(assessment.action, watchdog.ACTION_SURFACE)
        self.assertEqual(redrive_calls, [])

    def test_unreconciled_uncertain_action_blocks_reconstitution(self) -> None:
        job_id = self.register()
        self.record_dead_owner(job_id)
        action_id = self.ledger.begin_action(
            job_id, kind="implement", run_id="run-watchdog"
        )
        self.ledger.dispatch_action(action_id)
        self.ledger.mark_uncertain(action_id, detail="outcome unknown")
        redrive_calls: list[int] = []
        runner = self.runner(redrive=lambda _ledger, jid: redrive_calls.append(jid))
        assessment = runner.assess(self.job(job_id))
        self.assertEqual(assessment.action, watchdog.ACTION_BLOCKED)
        self.assertTrue(assessment.blocked)
        self.assertEqual(redrive_calls, [])

    def test_reconciliation_clears_the_block(self) -> None:
        job_id = self.register()
        self.record_dead_owner(job_id)
        action_id = self.ledger.begin_action(
            job_id, kind="implement", run_id="run-watchdog"
        )
        self.ledger.dispatch_action(action_id)
        self.ledger.mark_uncertain(action_id, detail="outcome unknown")
        redrive_calls: list[int] = []
        runner = self.runner(redrive=lambda _ledger, jid: redrive_calls.append(jid))
        self.assertEqual(runner.assess(self.job(job_id)).action, watchdog.ACTION_BLOCKED)
        self.ledger.reconcile_action(action_id)
        assessment = runner.assess(self.job(job_id))
        self.assertEqual(assessment.action, watchdog.ACTION_RECONSTITUTE)
        self.assertEqual(redrive_calls, [job_id])


class ExpectedHumanWaitTests(WatchdogTestCase):
    def test_no_action_is_taken_across_many_ticks(self) -> None:
        job_id = self.register()
        self.record_dead_owner(job_id)
        self.ledger.record_wait(
            job_id,
            kind="human",
            checkpoint="approval",
            material_hash="hash-1",
            change_id="change-a",
        )
        redrive_calls: list[int] = []
        runner = self.runner(redrive=lambda _ledger, jid: redrive_calls.append(jid))
        classifications: list[str] = []
        for _ in range(5):
            assessment = runner.assess(self.job(job_id))
            classifications.append(assessment.classification)
            self.assertEqual(assessment.action, watchdog.ACTION_NONE)
        self.assertEqual(
            set(classifications), {watchdog.CLASS_EXPECTED_HUMAN_WAIT}
        )
        self.assertEqual(redrive_calls, [])
        # Only the transition is recorded, not every idle tick.
        kinds = [str(row["kind"]) for row in self.ledger.list_watchdog_events(job_id)]
        self.assertEqual(kinds.count(watchdog.EVENT_CLASSIFICATION), 1)


class ReconstitutionTests(WatchdogTestCase):
    def test_dead_quiesced_job_is_reconstituted_and_records_prior_owner(self) -> None:
        job_id = self.register(state="active")
        self.record_dead_owner(job_id)
        redrive_calls: list[int] = []
        runner = self.runner(redrive=lambda _ledger, jid: redrive_calls.append(jid))
        assessment = runner.assess(self.job(job_id))
        self.assertEqual(assessment.classification, watchdog.CLASS_DEAD)
        self.assertEqual(assessment.action, watchdog.ACTION_RECONSTITUTE)
        self.assertEqual(redrive_calls, [job_id])
        events = self.ledger.list_watchdog_events(job_id)
        reconstituted = [
            row for row in events if str(row["kind"]) == watchdog.EVENT_RECONSTITUTED
        ]
        self.assertEqual(len(reconstituted), 1)
        detail = json.loads(reconstituted[0]["detail"])
        self.assertEqual(detail["prior_owner"], "opsx-plan supervise test")

    def test_reconstitution_is_refused_without_quiescence(self) -> None:
        job_id = self.register(state="active")
        self.record_live_owner(job_id)
        runner = self.runner()
        signals = runner.signals_for(self.job(job_id))
        with self.assertRaises(watchdog.WatchdogRefused):
            runner._reconstitute(self.job(job_id), signals, watchdog.CLASS_DEAD)

    def test_released_but_live_owner_blocks_reconstitution(self) -> None:
        """A released fencing record whose identity is live is not quiesced."""
        job_id = self.register(state="active")
        process = self.spawn_live_process()
        self.ledger.record_fencing(
            job_id,
            event="released",
            owner="opsx-plan supervise test",
            pid=process.pid,
            process_start=lock.process_start_time(process.pid),
            boot_id=lock.boot_identity(),
            host=None,
        )
        # The kernel-held flock is free (no lock file) yet the released identity
        # still matches a live process on this boot.
        self.assertFalse(lock.lock_file_path(self.repo).exists())
        owner = watchdog.recorded_owner(self.ledger, job_id)
        self.assertIsNotNone(owner)
        self.assertFalse(owner["held"])
        self.assertTrue(watchdog.owner_identity_is_live(owner))
        self.assertFalse(watchdog.verify_quiescence(self.repo, owner))

        redrive_calls: list[int] = []
        runner = self.runner(
            progress_window_seconds=10.0,
            stall_threshold_seconds=100.0,
            redrive=lambda _ledger, jid: redrive_calls.append(jid),
        )
        assessment = runner.assess(self.job(job_id))
        self.assertTrue(assessment.signals.liveness)
        self.assertFalse(assessment.signals.quiesced)
        self.assertNotEqual(assessment.classification, watchdog.CLASS_DEAD)
        self.assertEqual(assessment.action, watchdog.ACTION_SURFACE)
        self.assertTrue(assessment.blocked)
        # No re-drive, and the live prior owner is surfaced as a blocking event.
        self.assertEqual(redrive_calls, [])
        kinds = [str(row["kind"]) for row in self.ledger.list_watchdog_events(job_id)]
        self.assertIn(watchdog.EVENT_BLOCKED, kinds)

    def test_persisted_bounded_backoff_and_refusal_at_the_bound(self) -> None:
        base = datetime(2026, 1, 1, 12, 0, 0, tzinfo=UTC)
        current = {"value": _iso(base)}
        policy = _policy(
            budgets={
                "version": 1,
                "total_cost_usd": 100.0,
                "per_action_cost_usd": None,
                "total_elapsed_minutes": None,
                "per_action_elapsed_minutes": None,
                "max_incident_attempts": 2,
            }
        )
        with mock.patch.object(clock, "utcnow", side_effect=lambda: current["value"]):
            job_id = self.register(state="active", policy=policy)
            self.record_dead_owner(job_id)
            runner = self.runner(now=lambda: current["value"])
            signature = watchdog.restart_signature("watchdog-test")

            first = runner.assess(self.job(job_id))
            self.assertEqual(first.action, watchdog.ACTION_RECONSTITUTE)
            self.assertEqual(self.ledger.incident_attempt_count(job_id, signature), 1)

            # The durable backoff recomputes the next-allowed time from the count
            # and the last event, so an immediate second attempt waits.
            immediate = runner.assess(self.job(job_id))
            self.assertEqual(immediate.action, watchdog.ACTION_WAIT)

            current["value"] = _iso(base + timedelta(seconds=5))
            second = runner.assess(self.job(job_id))
            self.assertEqual(second.action, watchdog.ACTION_RECONSTITUTE)
            self.assertEqual(self.ledger.incident_attempt_count(job_id, signature), 2)

            current["value"] = _iso(base + timedelta(seconds=60))
            bounded = runner.assess(self.job(job_id))
            self.assertEqual(bounded.action, watchdog.ACTION_BLOCKED)
            self.assertTrue(bounded.blocked)
            self.assertEqual(self.ledger.incident_attempt_count(job_id, signature), 2)
            kinds = [str(row["kind"]) for row in self.ledger.list_watchdog_events(job_id)]
            self.assertIn(watchdog.EVENT_BOUNDED, kinds)

    def test_restart_count_survives_a_simulated_reset_and_a_reopen(self) -> None:
        job_id = self.register(state="active")
        signature = watchdog.restart_signature("watchdog-test")
        self.ledger.record_incident_attempt(job_id, signature=signature)
        self.ledger.record_incident_attempt(job_id, signature=signature)
        # A reset receipt is a durable reset marker; it must not re-baseline the
        # external attempt count.
        self.ledger.record_receipt(
            job_id,
            change_id="change-a",
            kind="reset",
            checkpoint="reset",
            material_hash="hash-1",
            authority="operator",
        )
        self.assertEqual(self.ledger.incident_attempt_count(job_id, signature), 2)
        self.ledger.close()
        self.ledger = self._open()
        self.assertEqual(self.ledger.incident_attempt_count(job_id, signature), 2)

    def test_events_are_append_only_and_survive_a_reopen(self) -> None:
        job_id = self.register()
        self.ledger.record_watchdog_event(
            job_id,
            kind=watchdog.EVENT_CLASSIFICATION,
            classification=watchdog.CLASS_DEAD,
            reason="classified dead",
        )
        self.ledger.record_watchdog_event(
            job_id,
            kind=watchdog.EVENT_RECONSTITUTED,
            classification=watchdog.CLASS_DEAD,
            reason="reconstituted",
            detail=json.dumps({"prior_owner": "worker-1"}),
        )
        with self.assertRaises(Exception):
            self.ledger.connection.execute(
                "UPDATE watchdog_events SET reason = 'rewritten'"
            )
        with self.assertRaises(Exception):
            self.ledger.connection.execute("DELETE FROM watchdog_events")
        self.ledger.close()
        self.ledger = self._open()
        events = self.ledger.list_watchdog_events(job_id)
        self.assertEqual(len(events), 2)
        self.assertEqual(str(events[0]["kind"]), watchdog.EVENT_CLASSIFICATION)
        self.assertEqual(str(events[1]["kind"]), watchdog.EVENT_RECONSTITUTED)
        self.assertEqual(
            json.loads(events[1]["detail"]), {"prior_owner": "worker-1"}
        )


class LoopbackSessionReconnectTests(WatchdogTestCase):
    """Reconnect-before-respawn against a loopback fake session API."""

    def _serve(self, sessions: set[str]) -> int:
        handler = type(
            "_Handler",
            (http.server.BaseHTTPRequestHandler,),
            {
                "sessions": sessions,
                "do_GET": _fake_session_get,
                "_emit": _emit_json,
                "log_message": lambda self, *args: None,
            },
        )
        server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), handler)
        port = int(server.server_address[1])
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        self.addCleanup(server.shutdown)
        self.addCleanup(server.server_close)
        return port

    def _link(self, job_id: int, port: int) -> None:
        session_bridge.record_primary_session_linkage(
            self.ledger,
            job_id,
            run_id="run-watchdog",
            server_address=f"127.0.0.1:{port}",
            session_id="session-1",
        )

    def test_live_session_is_adopted_over_loopback(self) -> None:
        from lib.orchestrator import supervision as supervision_mod

        port = self._serve({"session-1"})
        job_id = self.register(state="active")
        self.record_dead_owner(job_id)
        self._link(job_id, port)
        redrive_calls: list[int] = []
        runner = self.runner(
            reconnect=supervision_mod._watchdog_session_is_live,
            redrive=lambda _ledger, jid: redrive_calls.append(jid),
        )
        assessment = runner.boot_scan()[0]
        self.assertEqual(assessment.action, watchdog.ACTION_RECONNECT)
        self.assertEqual(redrive_calls, [])

    def test_absent_session_reconnects_to_nothing_and_respawns(self) -> None:
        from lib.orchestrator import supervision as supervision_mod

        port = self._serve(set())
        job_id = self.register(state="active")
        self.record_dead_owner(job_id)
        self._link(job_id, port)
        redrive_calls: list[int] = []
        runner = self.runner(
            reconnect=supervision_mod._watchdog_session_is_live,
            redrive=lambda _ledger, jid: redrive_calls.append(jid),
        )
        assessment = runner.boot_scan()[0]
        self.assertEqual(assessment.action, watchdog.ACTION_RECONSTITUTE)
        self.assertEqual(redrive_calls, [job_id])


def _fake_session_get(self) -> None:  # pragma: no cover - exercised via server
    if self.path == "/global/health":
        self._emit(200, {"healthy": True, "version": "1.18.5"})
        return
    if self.path.startswith("/session/"):
        session_id = self.path[len("/session/"):]
        if session_id in self.sessions:
            self._emit(200, {"id": session_id})
        else:
            self._emit(404, {"error": "not found"})
        return
    self._emit(404, {})


def _emit_json(self, status: int, payload: dict) -> None:  # pragma: no cover
    body = json.dumps(payload).encode("utf-8")
    self.send_response(status)
    self.send_header("Content-Type", "application/json")
    self.send_header("Content-Length", str(len(body)))
    self.end_headers()
    self.wfile.write(body)


class WatchdogObservationTests(WatchdogTestCase):
    def test_report_is_read_only_and_exposes_signals_and_restart(self) -> None:
        job_id = self.register(state="active")
        self.record_dead_owner(job_id)
        before_events = list(self.ledger.list_watchdog_events(job_id))
        runner = self.runner()
        report = runner.report(self.job(job_id))
        self.assertEqual(report["classification"], watchdog.CLASS_DEAD)
        self.assertIn("liveness", report["signals"])
        self.assertIn("progress", report["signals"])
        self.assertIn("deadline", report["signals"])
        self.assertEqual(report["restart"]["signature"], "watchdog_restart:watchdog-test")
        self.assertEqual(
            list(self.ledger.list_watchdog_events(job_id)), before_events
        )

    def test_watchdog_entries_stay_out_of_the_legacy_leaderboard(self) -> None:
        from lib.metrics import filter_leaderboard_records

        records = [
            {"role": "supervisor", "model": "cheap/model-a"},
            {"role": "implementer", "model": "cheap/model-a"},
            {"role": None, "model": "cheap/model-a"},
        ]
        filtered = filter_leaderboard_records(records)
        self.assertEqual([item["role"] for item in filtered], ["implementer", None])

    def test_unregistered_plan_exposes_no_watchdog_state(self) -> None:
        from lib.orchestrator import supervision as supervision_mod

        self.assertIsNone(
            supervision_mod.project_registered_job(self.repo, plan_name="watchdog-test")
        )

    def test_projection_exposes_read_only_watchdog_state(self) -> None:
        from lib.orchestrator import supervision as supervision_mod

        job_id = self.register(state="active")
        self.record_dead_owner(job_id)
        projection = supervision_mod.project_registered_job(
            self.repo, plan_name="watchdog-test"
        )
        self.assertIsNotNone(projection)
        state = projection["watchdog"]
        self.assertEqual(state["classification"], watchdog.CLASS_DEAD)
        self.assertIn("liveness", state["signals"])
        self.assertIn("progress", state["signals"])
        self.assertIn("deadline", state["signals"])
        # Read-only: nothing was recorded by building the observation state.
        self.assertEqual(self.ledger.list_watchdog_events(job_id), [])


class WatchdogCliTests(WatchdogTestCase):
    def _args(self, **overrides: object) -> argparse.Namespace:
        params = {
            "repo": str(self.repo),
            "plan": None,
            "store": str(self.db_path),
            "job_id": None,
            "once": True,
            "interval": 5.0,
            "json": True,
        }
        params.update(overrides)
        return argparse.Namespace(**params)

    def test_watchdog_once_json_reports_classification(self) -> None:
        job_id = self.register()
        self.ledger.record_wait(
            job_id,
            kind="human",
            checkpoint="approval",
            material_hash="hash-1",
            change_id="change-a",
        )
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            code = cmd_supervise.cmd_supervise_watchdog(self._args())
        self.assertEqual(code, 0)
        payload = json.loads(out.getvalue())
        self.assertEqual(payload["job_id"], job_id)
        self.assertEqual(
            payload["classification"], watchdog.CLASS_EXPECTED_HUMAN_WAIT
        )

    def test_watchdog_human_output(self) -> None:
        job_id = self.register()
        self.ledger.record_wait(
            job_id,
            kind="human",
            checkpoint="approval",
            material_hash="hash-1",
            change_id="change-a",
        )
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            code = cmd_supervise.cmd_supervise_watchdog(self._args(json=False))
        self.assertEqual(code, 0)
        self.assertIn("expected_human_wait", out.getvalue())

    def test_watchdog_unknown_job_fails_closed(self) -> None:
        err = io.StringIO()
        with contextlib.redirect_stderr(err):
            code = cmd_supervise.cmd_supervise_watchdog(self._args())
        self.assertEqual(code, 1)
        self.assertIn("UnknownJobError", err.getvalue())

    def test_watchdog_once_json_reports_every_non_terminal_job(self) -> None:
        first = self.register()
        other = self.repo / "other-worktree"
        other.mkdir()
        second = self.register(worktree=other)
        done = self.repo / "done-worktree"
        done.mkdir()
        terminal = self.register(worktree=done)
        self.ledger.set_job_state(terminal, "completed")

        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            code = cmd_supervise.cmd_supervise_watchdog(self._args())
        self.assertEqual(code, 0)
        payload = json.loads(out.getvalue())
        reported = {int(item["job_id"]) for item in payload["jobs"]}
        self.assertEqual(reported, {first, second})
        self.assertNotIn(terminal, reported)
        self.assertEqual(
            {int(item["job_id"]) for item in payload["assessments"]},
            {first, second},
        )

    def test_watchdog_job_id_selects_primary_without_narrowing_the_tick(self) -> None:
        first = self.register()
        other = self.repo / "other-worktree"
        other.mkdir()
        second = self.register(worktree=other)

        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            code = cmd_supervise.cmd_supervise_watchdog(self._args(job_id=second))
        self.assertEqual(code, 0)
        payload = json.loads(out.getvalue())
        self.assertEqual(payload["job_id"], second)
        self.assertEqual(
            {int(item["job_id"]) for item in payload["jobs"]},
            {first, second},
        )


def _now_after(created_at: str, seconds: float) -> datetime:
    parsed = datetime.fromisoformat(str(created_at))
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=UTC)
    return parsed + timedelta(seconds=seconds)


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
