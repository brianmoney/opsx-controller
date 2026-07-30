"""Verify that all global installers deploy the shared orchestrator runtime.

Each test uses a temporary HOME directory so the real user environment is
never modified.  The assertions cover:

* executables: ``opsx-plan``, ``opsx-run``, and ``opsx-watch-plan`` in
  ``~/.local/bin``
* runtime libraries: ``metrics``, ``pricing``, ``models`` under
  ``~/.local/lib/opsx-controller/lib``
* stale-install detection: the orchestrator's built-in check recognises
  a content mismatch between the installed executable and the repo copy
  independent of which installer deployed it.
"""

from __future__ import annotations

import hashlib
import importlib.util
import os
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest import mock

_THIS_FILE = Path(__file__).resolve()
_REPO = _THIS_FILE.parents[2]
_SCRIPT = _REPO / "orchestrator" / "opsx-plan.py"

_OPENCODE_INSTALLER = _REPO / "adapters" / "opencode" / "install.sh"
_CLAUDE_INSTALLER = _REPO / "adapters" / "claude-code" / "install.sh"
_CODEX_INSTALLER = _REPO / "adapters" / "codex-cli" / "install.sh"


def _model_env() -> dict[str, str]:
    """Return env vars that let `load_model_env` succeed for every adapter."""
    return {
        "OPSX_CONTROLLER_MODEL": "test-provider/test-controller",
        "OPSX_IMPLEMENTER_MODEL": "test-provider/test-implementer",
        "OPSX_REVIEWER_MODEL": "test-provider/test-reviewer",
        "OPSX_ARCHIVER_MODEL": "test-provider/test-archiver",
    }


def _run_installer(installer: Path, home: Path, env: dict[str, str]) -> None:
    """Run a global installer pointing at *home*.

    ``env`` must include ``HOME`` (pointing at *home*) plus the
    ``OPSX_*_MODEL`` overrides.
    """
    subprocess.run(
        ["bash", str(installer), "--global"],
        cwd=_REPO,
        env={**os.environ, **env},
        check=True,
        capture_output=True,
        text=True,
    )


class SharedInstallerHelperTests(unittest.TestCase):
    """Exercise the shared helper directly."""

    def setUp(self) -> None:
        self.home = tempfile.TemporaryDirectory()
        self.bin_dir = Path(self.home.name) / ".local" / "bin"
        self.lib_dir = Path(self.home.name) / ".local" / "lib" / "opsx-controller" / "lib"

    def tearDown(self) -> None:
        self.home.cleanup()

    def _run_helper(self) -> None:
        subprocess.run(
            ["bash", str(_REPO / "scripts" / "install-orchestrator.sh"), str(_REPO)],
            cwd=_REPO,
            env={**os.environ, "HOME": self.home.name},
            check=True,
            capture_output=True,
            text=True,
        )

    def test_helper_deploys_executables(self) -> None:
        self._run_helper()
        self.assertTrue(self.bin_dir.joinpath("opsx-plan").is_file())
        self.assertTrue(self.bin_dir.joinpath("opsx-run").is_file())
        self.assertTrue(self.bin_dir.joinpath("opsx-watch-plan").is_file())
        self.assertTrue(os.access(str(self.bin_dir / "opsx-watch-plan"), os.X_OK))

    def test_helper_deploys_runtime_libraries(self) -> None:
        self._run_helper()
        self.assertTrue(self.lib_dir.joinpath("metrics").is_dir())
        self.assertTrue(self.lib_dir.joinpath("pricing").is_dir())
        self.assertTrue(self.lib_dir.joinpath("models").is_dir())

    def test_executables_match_repo_copy(self) -> None:
        """Content-hash check: installed copy equals repository copy."""
        self._run_helper()
        installed = self.bin_dir / "opsx-plan"
        repo_copy = _REPO / "orchestrator" / "opsx-plan.py"
        self.assertEqual(
            hashlib.sha256(installed.read_bytes()).digest(),
            hashlib.sha256(repo_copy.read_bytes()).digest(),
        )

    def test_helper_deploys_watcher(self) -> None:
        """Verify the shared installer deploys opsx-watch-plan and preserves
        its content."""
        self._run_helper()
        watcher_path = self.bin_dir / "opsx-watch-plan"
        self.assertTrue(watcher_path.is_file(),
                        f"opsx-watch-plan missing in {self.bin_dir}")
        self.assertTrue(os.access(str(watcher_path), os.X_OK))
        repo_copy = _REPO / "scripts" / "opsx-watch-plan"
        self.assertEqual(
            hashlib.sha256(watcher_path.read_bytes()).digest(),
            hashlib.sha256(repo_copy.read_bytes()).digest(),
        )


class AdapterInstallerTests(unittest.TestCase):
    """Verify that every global installer deploys the shared runtime."""

    def setUp(self) -> None:
        self.home = tempfile.TemporaryDirectory()
        self.env = {**_model_env(), "HOME": self.home.name}

    def tearDown(self) -> None:
        self.home.cleanup()

    def _bin_dir(self) -> Path:
        return Path(self.home.name) / ".local" / "bin"

    def _lib_dir(self) -> Path:
        return Path(self.home.name) / ".local" / "lib" / "opsx-controller" / "lib"

    def _assert_executables_installed(self) -> None:
        """Verify opsx-plan, opsx-run, and opsx-watch-plan are present and
        executable."""
        opsx_plan = self._bin_dir() / "opsx-plan"
        opsx_run = self._bin_dir() / "opsx-run"
        opsx_watch = self._bin_dir() / "opsx-watch-plan"
        self.assertTrue(opsx_plan.is_file(),
                        f"opsx-plan missing in {self._bin_dir()}")
        self.assertTrue(opsx_run.is_file(),
                        f"opsx-run missing in {self._bin_dir()}")
        self.assertTrue(opsx_watch.is_file(),
                        f"opsx-watch-plan missing in {self._bin_dir()}")
        self.assertTrue(os.access(str(opsx_plan), os.X_OK))
        self.assertTrue(os.access(str(opsx_run), os.X_OK))
        self.assertTrue(os.access(str(opsx_watch), os.X_OK))

    def _assert_runtime_libraries_installed(self) -> None:
        lib = self._lib_dir()
        for pkg in ("metrics", "pricing", "models"):
            self.assertTrue(
                lib.joinpath(pkg).is_dir(),
                f"runtime library '{pkg}' missing in {lib}",
            )

    def test_opencode_global_install_deploys_runtime(self) -> None:
        _run_installer(_OPENCODE_INSTALLER, Path(self.home.name), self.env)
        self._assert_executables_installed()
        self._assert_runtime_libraries_installed()

    def test_claude_global_install_deploys_runtime(self) -> None:
        _run_installer(_CLAUDE_INSTALLER, Path(self.home.name), self.env)
        self._assert_executables_installed()
        self._assert_runtime_libraries_installed()

    def test_codex_global_install_deploys_runtime(self) -> None:
        _run_installer(_CODEX_INSTALLER, Path(self.home.name), self.env)
        self._assert_executables_installed()
        self._assert_runtime_libraries_installed()


class StaleInstallDetectionTests(unittest.TestCase):
    """_check_stale_install must work regardless of which installer deployed."""

    def setUp(self) -> None:
        self.home = tempfile.TemporaryDirectory()
        self.env = {**_model_env(), "HOME": self.home.name}

    def tearDown(self) -> None:
        self.home.cleanup()

    def _install_via(self, installer: Path) -> None:
        subprocess.run(
            ["bash", str(installer), "--global"],
            cwd=_REPO,
            env={**os.environ, **self.env},
            check=True,
            capture_output=True,
            text=True,
        )

    def _load_opsx_plan(self) -> object:
        spec = importlib.util.spec_from_file_location("opsx_plan", _SCRIPT)
        assert spec is not None
        assert spec.loader is not None
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        return module

    def _call_stale_check(self, opsx_plan: object, home: str) -> tuple[bool, str, str]:
        # _check_stale_install reads Path.home(), which is the real home
        # directory. Monkey-patch it so the check operates on the temp home
        # directory where the installer deployed.
        with mock.patch("pathlib.Path.home", return_value=Path(home)):
            return opsx_plan._check_stale_install(_REPO)

    def test_stale_detection_after_opencode_install(self) -> None:
        self._install_via(_OPENCODE_INSTALLER)
        ok, label, msg = self._call_stale_check(
            self._load_opsx_plan(), self.home.name
        )
        self.assertTrue(ok, f"stale check failed after opencode install: {msg}")

    def test_stale_detection_after_claude_install(self) -> None:
        self._install_via(_CLAUDE_INSTALLER)
        ok, label, msg = self._call_stale_check(
            self._load_opsx_plan(), self.home.name
        )
        self.assertTrue(ok, f"stale check failed after claude install: {msg}")

    def test_stale_detection_after_codex_install(self) -> None:
        self._install_via(_CODEX_INSTALLER)
        ok, label, msg = self._call_stale_check(
            self._load_opsx_plan(), self.home.name
        )
        self.assertTrue(ok, f"stale check failed after codex install: {msg}")

    def test_detects_content_mismatch_after_opencode_install(self) -> None:
        """Modify the installed copy after OpenCode install and assert mismatch is detected."""
        self._install_via(_OPENCODE_INSTALLER)
        installed = Path(self.home.name) / ".local" / "bin" / "opsx-plan"
        # Corrupt the installed copy
        installed.write_bytes(b"corrupted content")
        ok, label, msg = self._call_stale_check(
            self._load_opsx_plan(), self.home.name
        )
        self.assertFalse(ok, "stale check should detect content mismatch after opencode install")
        self.assertIn("stale", msg.lower())

    def test_detects_content_mismatch_after_claude_install(self) -> None:
        """Modify the installed copy after Claude Code install and assert mismatch is detected."""
        self._install_via(_CLAUDE_INSTALLER)
        installed = Path(self.home.name) / ".local" / "bin" / "opsx-plan"
        # Corrupt the installed copy
        installed.write_bytes(b"corrupted content")
        ok, label, msg = self._call_stale_check(
            self._load_opsx_plan(), self.home.name
        )
        self.assertFalse(ok, "stale check should detect content mismatch after claude install")
        self.assertIn("stale", msg.lower())

    def test_detects_content_mismatch_after_codex_install(self) -> None:
        """Modify the installed copy after Codex CLI install and assert mismatch is detected."""
        self._install_via(_CODEX_INSTALLER)
        installed = Path(self.home.name) / ".local" / "bin" / "opsx-plan"
        # Corrupt the installed copy
        installed.write_bytes(b"corrupted content")
        ok, label, msg = self._call_stale_check(
            self._load_opsx_plan(), self.home.name
        )
        self.assertFalse(ok, "stale check should detect content mismatch after codex install")
        self.assertIn("stale", msg.lower())


class WatcherBehaviorTests(unittest.TestCase):
    """Subprocess coverage for opsx-watch-plan following and switching."""

    def setUp(self) -> None:
        import time

        self.tmp = tempfile.TemporaryDirectory()
        self.repo = Path(self.tmp.name)
        self.log_dir = self.repo / ".opsx-plan" / "logs"
        self.log_dir.mkdir(parents=True)
        self.watcher_script = _REPO / "scripts" / "opsx-watch-plan"
        self.time = time

    def tearDown(self) -> None:
        self.tmp.cleanup()

    def _read_until(
        self, proc: subprocess.Popen, marker: str, timeout: float
    ) -> str:
        import select

        output = ""
        deadline = self.time.time() + timeout
        while self.time.time() < deadline:
            remaining = max(0.1, deadline - self.time.time())
            ready, _, _ = select.select(
                [proc.stdout], [], [], min(1.0, remaining)
            )
            if ready:
                try:
                    chunk = os.read(proc.stdout.fileno(), 4096)
                    if not chunk:
                        break
                    output += chunk.decode("utf-8", errors="replace")
                except (ValueError, OSError):
                    break
                if marker in output:
                    break
            if proc.poll() is not None:
                break
        return output

    def _start_watcher(self) -> subprocess.Popen:
        return subprocess.Popen(
            ["bash", str(self.watcher_script), str(self.repo)],
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
        )

    def _stop_watcher(self, proc: subprocess.Popen) -> None:
        proc.terminate()
        try:
            proc.wait(timeout=5)
        except subprocess.TimeoutExpired:
            proc.kill()
            proc.wait()
        # Close pipes to avoid ResourceWarning leaks.
        if proc.stdout:
            proc.stdout.close()
        if proc.stderr:
            proc.stderr.close()

    def test_watcher_follows_initial_log_and_switches_to_newer(self) -> None:
        """The watcher must detect a pre-existing log on startup and switch
        to a newer log when one appears."""
        # Create a pre-existing log for the watcher to find immediately.
        log1 = self.log_dir / "chg.implement.r1.1.log"
        log1.write_text("round 1 output\n")
        self.time.sleep(0.1)

        proc = self._start_watcher()
        try:
            # The watcher polls every 2 s.  Wait for it to report log1.
            out1 = self._read_until(proc, "chg.implement.r1.1.log", 10.0)
            self.assertIn(
                "chg.implement.r1.1.log",
                out1,
                f"watcher must discover the initial log; got: {out1[:400]}",
            )

            # Create a newer log; the watcher must switch to it.
            log2 = self.log_dir / "chg.implement.r2.1.log"
            log2.write_text("round 2 output\n")

            out2 = self._read_until(proc, "chg.implement.r2.1.log", 10.0)
            switched = (
                "chg.implement.r2.1.log" in out2
                or "switching" in out2
            )
            self.assertTrue(
                switched,
                f"watcher must switch to newer log; suffix: {out2[-400:]}",
            )
        finally:
            self._stop_watcher(proc)

    def test_watcher_picks_later_log_on_same_second_mtime_tie(self) -> None:
        """When a review log and a later implement log share the same mtime
        (same-second tie), the watcher must prefer the implement log created
        later, not the lexicographically-larger review log filename."""
        import os as _os

        # Simulate: r1 review log created, then same-second r2 implement log.
        review_log = self.log_dir / "chg.review.r1.1.log"
        review_log.write_text("review r1 findings\n")
        self.time.sleep(0.05)

        impl_log = self.log_dir / "chg.implement.r2.1.log"
        impl_log.write_text("implement r2 fixes\n")
        self.time.sleep(0.05)

        # Force both files to the same mtime so the tiebreaker fires.
        same_mtime: float = self.time.time()
        _os.utime(str(review_log), (same_mtime, same_mtime))
        _os.utime(str(impl_log), (same_mtime, same_mtime))

        proc = self._start_watcher()
        try:
        # The watcher scans all pre-existing logs.  The write order
        # does not matter here; the watcher selects the newest log
        # by the writer-guaranteed stage ordering key (round 2 is
        # newer than round 1 regardless of stage), so the implement
        # log must win even when both files share the same mtime.
            out = self._read_until(proc, "chg.implement.r2.1.log", 10.0)
            self.assertIn(
                "chg.implement.r2.1.log",
                out,
                f"watcher must prefer later-created implement log on same-second tie; got: {out[:400]}",
            )
            # Under the writer-guaranteed stage ordering key round 2
            # (implement) is newer than round 1 (review), so the
            # review log should NOT appear in the output.
            self.assertNotIn(
                "chg.review.r1.1.log",
                out,
                "review log must not be selected over the later implement log on a same-second tie",
            )
        finally:
            self._stop_watcher(proc)

    def test_watcher_reports_error_for_missing_log_directory(self) -> None:
        """The watcher must exit with a clear message when the log directory
        does not exist."""
        empty_repo = Path(self.tmp.name) / "no-logs"
        empty_repo.mkdir()

        proc = subprocess.run(
            ["bash", str(self.watcher_script), str(empty_repo)],
            capture_output=True,
            text=True,
        )
        self.assertNotEqual(proc.returncode, 0)
        self.assertIn("does not exist", proc.stderr)

    def test_same_round_review_to_archive_switch_on_same_mtime(self) -> None:
        """When review and archive logs share the same round and forced mtime,
        the watcher must select the archive log (stage_prio 2 > 1)."""
        import os as _os

        review_log = self.log_dir / "chg.review.r1.1.log"
        review_log.write_text("review r1 findings\n")
        self.time.sleep(0.05)

        archive_log = self.log_dir / "chg.archive.r1.1.log"
        archive_log.write_text("archive r1 done\n")
        self.time.sleep(0.05)

        # Force both files to the same mtime so the stage ordering tiebreaker fires.
        same_mtime: float = self.time.time()
        _os.utime(str(review_log), (same_mtime, same_mtime))
        _os.utime(str(archive_log), (same_mtime, same_mtime))

        proc = self._start_watcher()
        try:
            out = self._read_until(proc, "chg.archive.r1.1.log", 10.0)
            self.assertIn(
                "chg.archive.r1.1.log",
                out,
                f"archive log must win over review in same round on same mtime; got: {out[:400]}",
            )
            self.assertNotIn(
                "chg.review.r1.1.log",
                out,
                "review log must not be selected over archive log in same round on same mtime",
            )
        finally:
            self._stop_watcher(proc)

    def test_stage_ordering_implement_review_archive_same_round(self) -> None:
        """Within the same round, the watcher must prefer archive over review
        over implement when all share the same forced mtime."""
        import os as _os

        impl_log = self.log_dir / "chg.implement.r3.1.log"
        impl_log.write_text("implement r3\n")
        self.time.sleep(0.05)

        review_log = self.log_dir / "chg.review.r3.1.log"
        review_log.write_text("review r3\n")
        self.time.sleep(0.05)

        archive_log = self.log_dir / "chg.archive.r3.1.log"
        archive_log.write_text("archive r3\n")
        self.time.sleep(0.05)

        # Force all three to the same mtime.
        same_mtime: float = self.time.time()
        _os.utime(str(impl_log), (same_mtime, same_mtime))
        _os.utime(str(review_log), (same_mtime, same_mtime))
        _os.utime(str(archive_log), (same_mtime, same_mtime))

        proc = self._start_watcher()
        try:
            out = self._read_until(proc, "chg.archive.r3.1.log", 10.0)
            self.assertIn(
                "chg.archive.r3.1.log",
                out,
                f"archive must win over review and implement in same round; got: {out[:400]}",
            )
            self.assertNotIn(
                "chg.implement.r3.1.log",
                out,
                "implement log must not be selected over archive in same round",
            )
            self.assertNotIn(
                "chg.review.r3.1.log",
                out,
                "review log must not be selected over archive in same round",
            )
        finally:
            self._stop_watcher(proc)


if __name__ == "__main__":
    unittest.main()
