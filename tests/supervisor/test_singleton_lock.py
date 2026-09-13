"""Contract tests for the worktree execution lock (``lib/supervisor/lock.py``).

Covers mutual exclusion across process boundaries, verified-quiesced takeover,
identity semantics (PID reuse, prior boot, kernel-proof precedence), ledger
fencing persistence and the version-1 -> version-2 migration, lock-free
receipts/diagnostics, and legacy compatibility without a ledger.
"""

from __future__ import annotations

import argparse
import hashlib
import importlib.util
import os
import signal
import sqlite3
import subprocess
import sys
import tempfile
import textwrap
import time
import unittest
from pathlib import Path
from unittest import mock

from lib.supervisor import ledger, lock

REPO_ROOT = Path(__file__).resolve().parents[2]
SCRIPT = REPO_ROOT / "orchestrator" / "opsx-plan.py"

_STAGE_ENV = {
    "OPSX_CONTROLLER_MODEL": "test-provider/test-controller",
    "OPSX_IMPLEMENTER_MODEL": "test-provider/test-implementer",
    "OPSX_REVIEWER_MODEL": "test-provider/test-reviewer",
    "OPSX_ARCHIVER_MODEL": "test-provider/test-archiver",
}

_HOLDER_HELPER = textwrap.dedent(
    """
    import sys
    from pathlib import Path
    from lib.supervisor import ledger as ledger_module
    from lib.supervisor import lock as lock_module

    repo, ledger_path, job_id, owner, kind = sys.argv[1:6]
    handle = None
    if ledger_path != "-":
        handle = ledger_module.open_ledger(ledger_path, repository_root=repo)
    with lock_module.acquire(
        Path(repo),
        owner=owner,
        owner_kind=kind,
        job_id=int(job_id) if job_id != "-" else None,
        ledger=handle,
    ):
        sys.stdout.write("acquired\\n")
        sys.stdout.flush()
        line = sys.stdin.readline()
        if line.strip() == "release":
            pass
    if handle is not None:
        handle.close()
    """
)

_LIVE_FREE_FLOCK_HELPER = textwrap.dedent(
    """
    import fcntl
    import json
    import sys
    import time
    from pathlib import Path
    from lib.supervisor import lock as lock_module

    worktree, ready_path, go_path, kind = sys.argv[1:5]
    worktree = Path(worktree)

    # Hand-craft a record naming this *live* process, then acquire the flock.
    identity = lock_module.current_identity()
    record = {
        "version": 1,
        "state": lock_module.HELD,
        "owner": "live-holder",
        "owner_kind": kind,
        "job_id": None,
        "pid": identity["pid"],
        "process_start": identity["process_start"],
        "boot_id": identity["boot_id"],
        "host": identity["host"],
    }
    path = lock_module.record_path(worktree)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(record), encoding="utf-8")

    lock_path = lock_module.lock_file_path(worktree)
    fd = lock_path.open("a+")
    fcntl.flock(fd.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
    Path(ready_path).write_text("ready\\n", encoding="utf-8")

    # Wait until the parent asks us to drop the flock, then release it while
    # this process stays alive. The recorded identity is still live, so a
    # takeover attempt must be refused even though the flock is now free.
    deadline = time.time() + 30
    while time.time() < deadline:
        if Path(go_path).exists():
            break
        time.sleep(0.05)
    fcntl.flock(fd.fileno(), fcntl.LOCK_UN)
    fd.close()
    Path(go_path).write_text("free\\n", encoding="utf-8")
    while True:
        line = sys.stdin.readline()
        if line.strip() == "exit":
            break
    """
)


def _close_streams(proc: subprocess.Popen) -> None:
    """Close the helper's stdin/stdout/stderr after it has exited."""
    for stream in (proc.stdin, proc.stdout, proc.stderr):
        if stream is not None:
            try:
                stream.close()
            except OSError:
                pass


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


def _policy() -> dict:
    return {
        "authority_config": {"mode": "policy-bound"},
        "model_selection": {
            "version": 1,
            "roles": {"implementer": "cheap/model-a"},
            "stages": {"implement": "implementer"},
        },
        "inexpensive_allowlist": {
            "version": 1,
            "models": ["cheap/model-a"],
            "source": "test fixture",
        },
        "manifest_snapshot_hash": "deadbeef",
        "budgets": {"tokens": 1000},
        "deadlines": {"wall_seconds": 60},
    }


class LockTestCase(unittest.TestCase):
    """Shared temp layout: a git worktree and trusted ledger storage."""

    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.root = Path(self._tmp.name)
        self.repo = self.root / "repo"
        self.repo.mkdir()
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
        self.worktree = self.repo
        self.storage = self.root / "service-storage"
        self.storage.mkdir()
        self.db_path = self.storage / "supervisor.sqlite3"

        self.opsx_plan = load_opsx_plan()

    def open_ledger(self) -> ledger.Ledger:
        handle = ledger.open_ledger(self.db_path, repository_root=self.repo)
        self.addCleanup(handle.close)
        return handle

    def register(self, handle: ledger.Ledger, **overrides: object) -> int:
        params = {
            "run_id": "run-1",
            "worktree": self.worktree,
            "owner": "service",
            "policy": _policy(),
            "operator": "operator",
        }
        params.update(overrides)
        return handle.register_job(**params)

    def spawn_holder(
        self,
        *,
        ledger_path: Path | None = None,
        job_id: int | None = None,
        owner: str = "holder",
        kind: str = "ordinary",
    ) -> subprocess.Popen:
        env = dict(os.environ)
        env["PYTHONPATH"] = str(REPO_ROOT)
        proc = subprocess.Popen(
            [
                sys.executable,
                "-c",
                _HOLDER_HELPER,
                str(self.worktree),
                str(ledger_path) if ledger_path is not None else "-",
                str(job_id) if job_id is not None else "-",
                owner,
                kind,
            ],
            cwd=str(self.worktree),
            env=env,
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
        )
        self.addCleanup(self._kill)
        self._holder = proc
        assert proc.stdout is not None
        line = proc.stdout.readline()
        if line.strip() != "acquired":
            stderr = proc.stderr.read() if proc.stderr else ""
            self.fail(f"holder failed to acquire: {line!r} {stderr}")
        return proc

    def _kill(self) -> None:
        proc = getattr(self, "_holder", None)
        if proc is None:
            return
        if proc.poll() is None:
            try:
                proc.kill()
            except OSError:
                pass
            proc.wait(timeout=10)
        _close_streams(proc)

    def spawn_live_free_flock(self, *, kind: str = "ordinary") -> subprocess.Popen:
        """Spawn a live process that holds a hand-crafted record, then frees it.

        The process writes a fencing record naming *itself*, holds the flock,
        waits for the test to ask it to drop the flock, and then stays alive.
        That reproduces a live recorded identity with a free kernel lock.
        """
        ready = self.root / "live-ready"
        go = self.root / "live-go"
        env = dict(os.environ)
        env["PYTHONPATH"] = str(REPO_ROOT)
        proc = subprocess.Popen(
            [
                sys.executable,
                "-c",
                _LIVE_FREE_FLOCK_HELPER,
                str(self.worktree),
                str(ready),
                str(go),
                kind,
            ],
            cwd=str(self.worktree),
            env=env,
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
        )
        self.addCleanup(self._kill_live_free_flock, proc)
        deadline = time.time() + 15
        while time.time() < deadline:
            if ready.exists():
                break
            if proc.poll() is not None:
                stderr = proc.stderr.read() if proc.stderr else ""
                self.fail(f"live-free-flock helper exited early: {stderr}")
            time.sleep(0.02)
        else:
            self.fail("live-free-flock helper never signaled ready")
        return proc

    def drop_flock_and_wait(self, proc: subprocess.Popen) -> None:
        """Ask the helper to release the flock while remaining alive."""
        go = self.root / "live-go"
        go.write_text("go\n", encoding="utf-8")
        deadline = time.time() + 15
        while time.time() < deadline:
            if go.read_text(encoding="utf-8").strip() == "free":
                return
            if proc.poll() is not None:
                self.fail("live-free-flock helper died before freeing the lock")
            time.sleep(0.02)
        self.fail("live-free-flock helper never freed the lock")

    def _kill_live_free_flock(self, proc: subprocess.Popen) -> None:
        if proc.poll() is None:
            try:
                proc.kill()
            except OSError:
                pass
            proc.wait(timeout=10)
        _close_streams(proc)


class MutualExclusionTests(LockTestCase):
    def test_in_process_second_acquisition_is_refused(self) -> None:
        with lock.acquire(self.worktree, owner="first"):
            with self.assertRaises(lock.LockContentionError):
                with lock.acquire(self.worktree, owner="second"):
                    pass

    def test_supervised_record_refuses_ordinary_with_named_error(self) -> None:
        with lock.acquire(
            self.worktree, owner="svc", owner_kind="supervised", job_id=7
        ):
            with self.assertRaises(lock.SupervisedOwnershipError):
                with lock.acquire(self.worktree, owner="ordinary"):
                    pass

    def test_live_supervised_record_refuses_second_supervised_job(self) -> None:
        with lock.acquire(
            self.worktree, owner="svc", owner_kind="supervised", job_id=1
        ):
            with self.assertRaises(lock.SupervisedOwnershipError):
                with lock.acquire(
                    self.worktree, owner="svc2", owner_kind="supervised", job_id=2
                ):
                    pass

    def test_subprocess_holder_blocks_second_module_acquisition(self) -> None:
        self.spawn_holder(owner="external")
        with self.assertRaises(lock.LockContentionError):
            with lock.acquire(self.worktree, owner="local"):
                pass


class ProcessBoundaryTests(LockTestCase):
    def _plan_and_state(self) -> None:
        plans = self.repo / "openspec" / "plans"
        plans.mkdir(parents=True, exist_ok=True)
        (plans / "test.toml").write_text(
            textwrap.dedent(
                """\
                [plan]
                name = "test-plan"
                adapter = "opencode"

                [[changes]]
                id = "change-a"
                """
            ),
            encoding="utf-8",
        )

    def _subprocess(self, argv: list[str]) -> subprocess.CompletedProcess:
        env = dict(os.environ)
        env.update(_STAGE_ENV)
        env["PYTHONPATH"] = str(REPO_ROOT)
        return subprocess.run(
            [sys.executable, *argv],
            cwd=str(self.repo),
            env=env,
            capture_output=True,
            text=True,
        )

    def test_subprocess_reset_blocked_with_no_state_mutation(self) -> None:
        self._plan_and_state()
        state = self.opsx_plan.state_mod.load_state(self.repo, "test-plan")
        self.opsx_plan.state_mod.set_status(
            state, "change-a", self.opsx_plan.base.FAILED, "test failure"
        )
        self.opsx_plan.state_mod.save_state(self.repo, "test-plan", state)
        state_path = self.opsx_plan.state_mod.state_path(self.repo, "test-plan")
        before = state_path.read_bytes()

        self.spawn_holder(owner="external")
        result = self._subprocess(
            [
                str(SCRIPT),
                "--repo",
                str(self.repo),
                "reset",
                "openspec/plans/test.toml",
                "change-a",
            ]
        )
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("already held", result.stderr)
        self.assertEqual(state_path.read_bytes(), before)

    def test_both_names_contend_with_held_lock(self) -> None:
        change = self.repo / "openspec" / "changes" / "change-a"
        change.mkdir(parents=True, exist_ok=True)
        (change / "proposal.md").write_text("## Why\n", encoding="utf-8")
        (change / "tasks.md").write_text("- [ ] 1.1 task\n", encoding="utf-8")

        self.spawn_holder(owner="external")

        run_one = self._subprocess(
            [str(SCRIPT), "--repo", str(self.repo), "run-one", "change-a"]
        )
        self.assertNotEqual(run_one.returncode, 0)
        self.assertIn("already held", run_one.stderr)

        exe_link = self.root / "opsx-run"
        os.symlink(SCRIPT, exe_link)
        run_exe = self._subprocess(
            [str(exe_link), "change-a", "--repo", str(self.repo)]
        )
        self.assertNotEqual(run_exe.returncode, 0)
        self.assertIn("already held", run_exe.stderr)

    def test_exe_name_dispatch_adds_no_second_acquisition(self) -> None:
        """A single acquisition succeeds; a second in-process flock would not.

        The dirty-tree refusal happens *after* the lock is acquired, so
        reaching it proves the handler acquired the lock exactly once.
        """
        change = self.repo / "openspec" / "changes" / "change-a"
        change.mkdir(parents=True, exist_ok=True)
        (change / "proposal.md").write_text("## Why\n", encoding="utf-8")
        (change / "tasks.md").write_text("- [ ] 1.1 task\n", encoding="utf-8")
        (self.repo / "tracked.txt").write_text("dirty\n", encoding="utf-8")

        exe_link = self.root / "opsx-run"
        os.symlink(SCRIPT, exe_link)
        result = self._subprocess(
            [str(exe_link), "change-a", "--repo", str(self.repo)]
        )
        self.assertEqual(result.returncode, 2)
        self.assertIn("tracked worktree is dirty", result.stderr)
        self.assertNotIn("already held", result.stderr)


class TakeoverTests(LockTestCase):
    def test_killed_holder_is_fenced_and_replaced(self) -> None:
        handle = self.open_ledger()
        job_id = self.register(handle)
        handle.close()

        holder = self.spawn_holder(
            ledger_path=self.db_path, job_id=job_id, owner="worker", kind="supervised"
        )
        holder.kill()
        holder.wait(timeout=10)

        ledger2 = self.open_ledger()
        with lock.acquire(
            self.worktree,
            owner="replacement",
            owner_kind="supervised",
            job_id=job_id,
            ledger=ledger2,
        ):
            pass
        events = [row["event"] for row in ledger2.list_fencing(job_id)]
        self.assertIn("fenced", events)
        self.assertIn("acquired", events)
        fenced = [row for row in ledger2.list_fencing(job_id) if row["event"] == "fenced"][0]
        self.assertEqual(fenced["owner"], "worker")
        self.assertIsNotNone(fenced["pid"])
        self.assertIsNotNone(fenced["process_start"])
        self.assertIsNotNone(fenced["boot_id"])
        # The fenced row names the fenced identity; the following acquired row
        # names the replacing identity, so both are visible in the history.
        acquired = [row for row in ledger2.list_fencing(job_id) if row["event"] == "acquired"][-1]
        self.assertEqual(acquired["owner"], "replacement")

    def test_clean_release_records_no_fencing(self) -> None:
        handle = self.open_ledger()
        job_id = self.register(handle)
        handle.close()

        holder = self.spawn_holder(
            ledger_path=self.db_path, job_id=job_id, owner="worker", kind="supervised"
        )
        assert holder.stdin is not None
        holder.stdin.write("release\n")
        holder.stdin.flush()
        holder.wait(timeout=10)
        self.assertEqual(holder.returncode, 0)
        _close_streams(holder)

        ledger2 = self.open_ledger()
        with lock.acquire(
            self.worktree,
            owner="next",
            owner_kind="supervised",
            job_id=job_id,
            ledger=ledger2,
        ):
            pass
        events = [row["event"] for row in ledger2.list_fencing(job_id)]
        self.assertEqual(events[:3], ["acquired", "released", "acquired"])
        self.assertNotIn("fenced", events)

    def test_live_owner_refuses_takeover(self) -> None:
        handle = self.open_ledger()
        job_id = self.register(handle)
        handle.close()

        holder = self.spawn_holder(
            ledger_path=self.db_path, job_id=job_id, owner="worker", kind="supervised"
        )
        with self.assertRaises(lock.SupervisedOwnershipError):
            with lock.acquire(
                self.worktree,
                owner="usurper",
                owner_kind="supervised",
                job_id=job_id,
                ledger=self.open_ledger(),
            ):
                pass
        assert holder.stdin is not None
        holder.stdin.write("release\n")
        holder.stdin.flush()
        holder.wait(timeout=10)
        _close_streams(holder)

    def test_live_record_with_free_flock_refuses_ordinary_takeover(self) -> None:
        proc = self.spawn_live_free_flock(kind="ordinary")
        self.drop_flock_and_wait(proc)
        self.assertTrue(lock.holder_is_live(lock.read_record(self.worktree)))
        with self.assertRaises(lock.LockContentionError):
            with lock.acquire(self.worktree, owner="replacement"):
                pass
        self._exit_live_free_flock(proc)
        with lock.acquire(self.worktree, owner="replacement"):
            pass

    def test_live_supervised_record_with_free_flock_refuses_takeover(self) -> None:
        proc = self.spawn_live_free_flock(kind="supervised")
        self.drop_flock_and_wait(proc)
        self.assertTrue(lock.holder_is_live(lock.read_record(self.worktree)))
        with self.assertRaises(lock.SupervisedOwnershipError):
            with lock.acquire(self.worktree, owner="replacement"):
                pass
        self._exit_live_free_flock(proc)
        with lock.acquire(self.worktree, owner="replacement"):
            pass

    def _exit_live_free_flock(self, proc: subprocess.Popen) -> None:
        assert proc.stdin is not None
        proc.stdin.write("exit\n")
        proc.stdin.flush()
        proc.wait(timeout=10)
        self.assertEqual(proc.returncode, 0)
        _close_streams(proc)


class IdentitySemanticsTests(LockTestCase):
    def _write_raw_record(self, **overrides: object) -> None:
        record = {
            "version": 1,
            "state": lock.HELD,
            "owner": "crafted",
            "owner_kind": "ordinary",
            "job_id": None,
            "pid": os.getpid(),
            "process_start": lock.process_start_time(os.getpid()),
            "boot_id": lock.boot_identity(),
            "host": "test",
        }
        record.update(overrides)
        path = lock.record_path(self.worktree)
        path.parent.mkdir(parents=True, exist_ok=True)
        import json

        path.write_text(json.dumps(record), encoding="utf-8")

    def test_reused_pid_with_mismatched_start_is_not_live(self) -> None:
        observed = lock.process_start_time(os.getpid())
        self.assertIsNotNone(observed)
        self._write_raw_record(process_start=float(observed) + 1000.0)
        self.assertFalse(lock.holder_is_live(lock.read_record(self.worktree)))

    def test_previous_boot_record_is_stale(self) -> None:
        start = lock.process_start_time(os.getpid())
        self._write_raw_record(boot_id="00000000-0000-0000-0000-000000000000", process_start=start)
        record = lock.read_record(self.worktree)
        self.assertFalse(lock.holder_is_live(record))
        with lock.acquire(self.worktree, owner="next"):
            pass

    def test_kernel_proof_wins_over_live_looking_record(self) -> None:
        # A hand-crafted record claims our own identity, but the flock is free:
        # the identity matches the acquirer, so this is not a distinct prior
        # holder and acquisition proceeds. A *different* live identity with a
        # free flock is refused (see TakeoverTests).
        self._write_raw_record()
        self.assertTrue(lock.holder_is_live(lock.read_record(self.worktree)))
        with lock.acquire(self.worktree, owner="next"):
            pass

    def test_different_live_identity_with_free_flock_is_refused(self) -> None:
        # PID 1 is necessarily live; record its real start time so the
        # identity matches, then hand-craft the held record with the flock free.
        start = lock.process_start_time(1)
        if start is None:
            self.skipTest("cannot observe PID 1 start time on this platform")
        self._write_raw_record(pid=1, process_start=start, owner="other")
        prior = lock.read_record(self.worktree)
        self.assertTrue(lock.holder_is_live(prior))
        with self.assertRaises(lock.LockContentionError):
            with lock.acquire(self.worktree, owner="next"):
                pass

    def test_bare_pid_without_start_is_not_proof(self) -> None:
        self._write_raw_record(process_start=None)
        self.assertFalse(lock.holder_is_live(lock.read_record(self.worktree)))

    def test_missing_recorded_boot_identity_falls_back_to_flock(self) -> None:
        # The recorded start time matches a live process, but the record
        # carries no boot identity: liveness must not be inferred from
        # PID/start time alone, so a free flock permits acquisition.
        self._write_raw_record(boot_id=None)
        self.assertFalse(lock.holder_is_live(lock.read_record(self.worktree)))
        with lock.acquire(self.worktree, owner="next"):
            pass

    def test_missing_current_boot_identity_falls_back_to_flock(self) -> None:
        # A record names this live identity, but the platform exposes no
        # current boot identity to compare against: identity liveness is
        # unknowable and only the kernel flock arbitrates.
        self._write_raw_record()
        with mock.patch.object(lock, "boot_identity", return_value=None):
            self.assertFalse(lock.holder_is_live(lock.read_record(self.worktree)))
            with lock.acquire(self.worktree, owner="next"):
                pass

    def test_missing_observed_process_start_falls_back_to_flock(self) -> None:
        # The record names a live identity but the observed process start time
        # is unavailable: liveness cannot be established without both
        # discriminators.
        self._write_raw_record()
        with mock.patch.object(lock, "process_start_time", return_value=None):
            self.assertFalse(lock.holder_is_live(lock.read_record(self.worktree)))
            with lock.acquire(self.worktree, owner="next"):
                pass


class LedgerPersistenceTests(LockTestCase):
    def test_supervised_acquisition_survives_reopen(self) -> None:
        handle = self.open_ledger()
        job_id = self.register(handle)
        with lock.acquire(
            self.worktree,
            owner="svc",
            owner_kind="supervised",
            job_id=job_id,
            ledger=handle,
        ):
            pass
        events = [row["event"] for row in handle.list_fencing(job_id)]
        self.assertEqual(events, ["acquired", "released"])
        handle.close()

        reopened = self.open_ledger()
        rows = reopened.list_fencing(job_id)
        self.assertEqual(len(rows), 2)
        self.assertEqual(rows[1]["event"], "released")

    def test_version_one_ledger_migrates_preserving_records(self) -> None:
        handle = self.open_ledger()
        job_id = self.register(handle)
        handle.close()

        # Simulate a ledger written before fencing records existed.
        conn = sqlite3.connect(self.db_path)
        conn.execute("DROP TABLE fencing_records")
        conn.execute("PRAGMA user_version = 1")
        conn.commit()
        conn.close()

        migrated = self.open_ledger()
        self.assertEqual(migrated.schema_version(), ledger.CURRENT_SCHEMA_VERSION)
        self.assertEqual(migrated.get_job(job_id)["id"], job_id)
        migrated.record_fencing(job_id, event="acquired", owner="svc")
        self.assertEqual(len(migrated.list_fencing(job_id)), 1)


class ReleaseDurabilityTests(LockTestCase):
    def test_release_record_write_failure_is_surfaced_after_cleanup(self) -> None:
        context = lock.acquire(self.worktree, owner="holder")
        handle = context.__enter__()
        try:
            with mock.patch.object(
                lock, "_write_record", side_effect=OSError("disk full")
            ):
                with self.assertRaises(lock.LockReleaseError) as ctx:
                    handle.release()
        finally:
            context.__exit__(None, None, None)
        self.assertIn("fencing record write failed", str(ctx.exception))
        # Cleanup still happened: the lock is free for the next acquirer.
        with lock.acquire(self.worktree, owner="next"):
            pass

    def test_release_ledger_failure_is_surfaced_after_cleanup(self) -> None:
        handle = self.open_ledger()
        job_id = self.register(handle)
        context = lock.acquire(
            self.worktree,
            owner="svc",
            owner_kind="supervised",
            job_id=job_id,
            ledger=handle,
        )
        lock_handle = context.__enter__()
        try:
            with mock.patch.object(
                handle, "record_fencing", side_effect=RuntimeError("ledger down")
            ):
                with self.assertRaises(lock.LockReleaseError) as ctx:
                    lock_handle.release()
        finally:
            context.__exit__(None, None, None)
        self.assertIn("ledger release event failed", str(ctx.exception))
        # The released fencing record on disk is still durable.
        record = lock.read_record(self.worktree)
        self.assertEqual(record["state"], lock.RELEASED)
        with lock.acquire(self.worktree, owner="next"):
            pass

    def test_successful_release_reports_no_failure(self) -> None:
        with lock.acquire(self.worktree, owner="holder"):
            pass
        record = lock.read_record(self.worktree)
        self.assertEqual(record["state"], lock.RELEASED)


class ReceiptAndDiagnosticsTests(LockTestCase):
    def _gated_plan(self) -> None:
        plans = self.repo / "openspec" / "plans"
        plans.mkdir(parents=True, exist_ok=True)
        (plans / "test.toml").write_text(
            textwrap.dedent(
                """\
                [plan]
                name = "test-plan"
                adapter = "opencode"

                [[changes]]
                id = "gated-a"
                pause_before = true
                """
            ),
            encoding="utf-8",
        )
        self.opsx_plan.write_active_plan(self.repo, "openspec/plans/test.toml")

    def test_approve_succeeds_while_lock_held(self) -> None:
        self._gated_plan()
        with lock.acquire(self.worktree, owner="holder"):
            rc = self.opsx_plan.cmd_gates.cmd_approve(
                argparse.Namespace(
                    repo=str(self.repo), plan=None, change=["gated-a"], approve_all=False
                )
            )
            self.assertEqual(rc, 0)
        state = self.opsx_plan.state_mod.load_state(self.repo, "test-plan")
        self.assertIn("gated-a", state["approvals"])

    def test_status_runs_while_lock_held(self) -> None:
        self._gated_plan()
        with lock.acquire(self.worktree, owner="holder"):
            rc = self.opsx_plan.cmd_status.cmd_status(
                argparse.Namespace(repo=str(self.repo), plan=None)
            )
            self.assertEqual(rc, 0)


class LegacyCompatibilityTests(LockTestCase):
    def test_ordinary_acquisition_needs_no_ledger(self) -> None:
        self.assertFalse(self.db_path.exists())
        with lock.acquire(self.worktree, owner="legacy"):
            pass
        self.assertFalse(self.db_path.exists())

    def test_held_supervised_lock_refuses_then_succeeds_after_release(self) -> None:
        context = lock.acquire(
            self.worktree, owner="svc", owner_kind="supervised", job_id=9
        )
        with context:
            with self.assertRaises(lock.SupervisedOwnershipError):
                with lock.acquire(self.worktree, owner="legacy"):
                    pass
        with lock.acquire(self.worktree, owner="legacy"):
            pass


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
