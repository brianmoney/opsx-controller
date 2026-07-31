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
        log1 = self.log_dir / "chg.implement.r1.1.log"
        log1.write_text("round 1 output\n")
        self.time.sleep(0.1)

        proc = self._start_watcher()
        try:
            out1 = self._read_until(proc, "round 1 output", 10.0)
            self.assertIn(
                "round 1 output",
                out1,
                f"watcher must discover the initial log; got: {out1[:400]}",
            )

            log2 = self.log_dir / "chg.implement.r2.1.log"
            log2.write_text("round 2 output ZXCVB\n")

            out2 = self._read_until(proc, "ZXCVB", 10.0)
            self.assertIn(
                "ZXCVB",
                out2,
                f"watcher must switch to newer log; suffix: {out2[-400:]}",
            )
        finally:
            self._stop_watcher(proc)

    def test_watcher_picks_later_log_on_same_second_mtime_tie(self) -> None:
        """When a review log and a later implement log share the same mtime
        (same-second tie), the watcher must prefer the implement log created
        later, not the lexicographically-larger review log filename."""
        import os as _os

        review_log = self.log_dir / "chg.review.r1.1.log"
        review_log.write_text("review r1 findings XYZ\n")
        self.time.sleep(0.05)

        impl_log = self.log_dir / "chg.implement.r2.1.log"
        impl_log.write_text("implement r2 fixes XYZ\n")
        self.time.sleep(0.05)

        same_mtime: float = self.time.time()
        _os.utime(str(review_log), (same_mtime, same_mtime))
        _os.utime(str(impl_log), (same_mtime, same_mtime))

        proc = self._start_watcher()
        try:
            out = self._read_until(proc, "r2 fixes", 10.0)
            self.assertIn(
                "implement r2 fixes XYZ",
                out,
                f"watcher must prefer later-created implement log on same-second tie; got: {out[:400]}",
            )
            self.assertNotIn(
                "review r1 findings XYZ",
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

        same_mtime: float = self.time.time()
        _os.utime(str(review_log), (same_mtime, same_mtime))
        _os.utime(str(archive_log), (same_mtime, same_mtime))

        proc = self._start_watcher()
        try:
            out = self._read_until(proc, "archive r1", 10.0)
            self.assertIn(
                "archive r1 done",
                out,
                f"archive log must win over review in same round on same mtime; got: {out[:400]}",
            )
            self.assertNotIn(
                "review r1 findings",
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

        same_mtime: float = self.time.time()
        _os.utime(str(impl_log), (same_mtime, same_mtime))
        _os.utime(str(review_log), (same_mtime, same_mtime))
        _os.utime(str(archive_log), (same_mtime, same_mtime))

        proc = self._start_watcher()
        try:
            out = self._read_until(proc, "archive r3", 10.0)
            self.assertIn(
                "archive r3",
                out,
                f"archive must win over review and implement in same round; got: {out[:400]}",
            )
            self.assertNotIn(
                "implement r3",
                out,
                "implement log must not be selected over archive in same round",
            )
            self.assertNotIn(
                "review r3",
                out,
                "review log must not be selected over archive in same round",
            )
        finally:
            self._stop_watcher(proc)

    # ------------------------------------------------------------------
    # 6.1 newly-appeared log is emitted exactly once
    def test_newly_appeared_log_emitted_exactly_once(self) -> None:
        proc = self._start_watcher()
        try:
            marker = "UNIQUE_MARKER_6_1_ABCDEF"
            log = self.log_dir / "chg.implement.r1.1.log"
            log.write_text(f"{marker}\n")
            self.time.sleep(3.0)

            out = self._read_until(proc, marker, 10.0)
            count = out.count(marker)
            self.assertEqual(
                1,
                count,
                f"distinctive line must appear exactly once; found {count}; output:\n{out[:500]}",
            )
        finally:
            self._stop_watcher(proc)

    # ------------------------------------------------------------------
    # 6.2 stage banner with a real LATEST_FIX_PROMPT
    def test_stage_banner_shows_fix_prompt(self) -> None:
        log = self.log_dir / "chg.implement.r1.1.log"
        log.write_text(
            "# --- OPSX WORKER INPUT ---\n"
            "# CHANGE: mychange\n"
            "# ROUND: 1\n"
            "# STATE_FILE: /tmp/s\n"
            "# LATEST_FIX_PROMPT: Find the bug CORRECTIVE GUIDANCE: fix it VERIFY: test\n"
            "# TASK_COUNTS: 0/10\n"
            "# CONTEXT_CACHE_STATUS: valid\n"
            "# CONTEXT_CACHE_VALID: true\n"
            "# CONTEXT_CACHE_SUMMARY: summary\n"
            "# --- END OPSX WORKER INPUT ---\n"
            "some worker output\n"
        )
        self.time.sleep(0.1)

        proc = self._start_watcher()
        try:
            out = self._read_until(proc, "| VERIFY:", 10.0)
            self.assertIn("Stage:", out, f"expected stage banner; got: {out[:500]}")
            self.assertIn("mychange", out)
            self.assertIn("implement", out)
            self.assertIn("Find the bug", out)
            self.assertIn("fix it", out)
            self.assertIn("test", out)
        finally:
            self._stop_watcher(proc)

    # ------------------------------------------------------------------
    # 6.3 stage banner with LATEST_FIX_PROMPT: none omits fix-prompt section
    def test_stage_banner_omits_fix_prompt_when_none(self) -> None:
        log = self.log_dir / "chg.implement.r1.1.log"
        log.write_text(
            "# --- OPSX WORKER INPUT ---\n"
            "# CHANGE: mychange\n"
            "# ROUND: 1\n"
            "# STATE_FILE: /tmp/s\n"
            "# LATEST_FIX_PROMPT: none\n"
            "# TASK_COUNTS: 0/10\n"
            "# CONTEXT_CACHE_STATUS: valid\n"
            "# CONTEXT_CACHE_VALID: true\n"
            "# CONTEXT_CACHE_SUMMARY: summary\n"
            "# --- END OPSX WORKER INPUT ---\n"
            "some worker output\n"
        )
        self.time.sleep(0.1)

        proc = self._start_watcher()
        try:
            out = self._read_until(proc, "Stage:", 10.0)
            banner_section = out[out.find("Stage:"):]
            self.assertIn("Stage:", banner_section)
            self.assertIn("mychange", banner_section)
            self.assertNotIn(
                "FINDINGS",
                banner_section,
                f"fix-prompt section must be absent when LATEST_FIX_PROMPT: none; banner:\n{banner_section[:500]}",
            )
        finally:
            self._stop_watcher(proc)

    # ------------------------------------------------------------------
    # 6.4 failing verdict banner with fix prompt
    def test_verdict_banner_failing_review(self) -> None:
        log = self.log_dir / "chg.review.r1.1.log"
        log.write_text("review log starts\n")
        self.time.sleep(0.1)

        proc = self._start_watcher()
        try:
            out = self._read_until(proc, "review log starts", 10.0)
            self.assertIn("review log starts", out)

            verdict_json = (
                '{"status":"reviewed","change":"chg","round":1,'
                '"verdict":"fail",'
                '"finding_counts":{"critical":2,"warning":1,"note":0},'
                '"summary":"needs fixes",'
                '"fix_prompt":"CHANGE: chg\\nFINDINGS:\\n- [critical] missing test\\n'
                'CORRECTIVE GUIDANCE:\\nadd tests\\nVERIFY:\\nrun tests",'
                '"next_phase":"implement"}'
            )
            with open(str(log), "a") as f:
                f.write(verdict_json + "\n")
                f.flush()

            out2 = self._read_until(proc, "| CORRECTIVE GUIDANCE:", 15.0)
            self.assertIn("FAIL", out2, f"expected FAIL verdict; got: {out2[:500]}")
            self.assertIn("Critical: 2", out2)
            self.assertIn("Warning: 1", out2)
            self.assertIn("Note: 0", out2)
            self.assertIn("needs fixes", out2)
            self.assertIn("implement", out2)
            self.assertIn("missing test", out2)
            self.assertIn("add tests", out2)
            self.assertIn("FINDINGS", out2)
            self.assertIn("CORRECTIVE GUIDANCE", out2)
            self.assertIn("VERIFY", out2)
            self.assertIn("[critical]", out2)
            self.assertIn(verdict_json.split(",")[0], out2,
                          "raw JSON line must be present unchanged")
        finally:
            self._stop_watcher(proc)

    # ------------------------------------------------------------------
    # 6.5 passing verdict banner has no fix-prompt section
    def test_verdict_banner_passing_review(self) -> None:
        log = self.log_dir / "chg.review.r1.1.log"
        log.write_text("review log starts\n")
        self.time.sleep(0.1)

        proc = self._start_watcher()
        try:
            out = self._read_until(proc, "review log starts", 10.0)

            verdict_json = (
                '{"status":"reviewed","change":"chg","round":1,'
                '"verdict":"pass",'
                '"finding_counts":{"critical":0,"warning":0,"note":0},'
                '"summary":"all good",'
                '"fix_prompt":"",'
                '"next_phase":"archive"}'
            )
            with open(str(log), "a") as f:
                f.write(verdict_json + "\n")
                f.flush()

            out2 = self._read_until(proc, "Critical:", 15.0)
            self.assertIn("PASS", out2, f"expected PASS verdict; got: {out2[:500]}")
            self.assertIn("Critical: 0", out2)
            self.assertIn("all good", out2)
            self.assertIn("archive", out2)
            banner = out2[out2.find("Review:"):]
            self.assertNotIn(
                "FINDINGS",
                banner,
                f"fix-prompt section must be absent for passing review; banner:\n{banner[:500]}",
            )
        finally:
            self._stop_watcher(proc)

    # ------------------------------------------------------------------
    # 6.6 structured fix prompt renders sections and markers on separate lines
    def test_structured_fix_prompt_section_splitting(self) -> None:
        log = self.log_dir / "chg.review.r1.1.log"
        log.write_text("review log starts\n")
        self.time.sleep(0.1)

        proc = self._start_watcher()
        try:
            out = self._read_until(proc, "review log starts", 10.0)

            verdict_json = (
                '{"status":"reviewed","change":"chg","round":1,'
                '"verdict":"fail",'
                '"finding_counts":{"critical":1,"warning":1,"note":1},'
                '"summary":"structured test",'
                '"fix_prompt":"CHANGE: chg\\nFINDINGS:\\n- [critical] crit issue\\n'
                '- [warning] warn issue\\n- [note] note issue\\n'
                'CORRECTIVE GUIDANCE:\\nplease fix all\\nVERIFY:\\nall tests pass",'
                '"next_phase":"implement"}'
            )
            with open(str(log), "a") as f:
                f.write(verdict_json + "\n")
                f.flush()

            out2 = self._read_until(proc, "| VERIFY:", 15.0)
            banner = out2[out2.find("Review:"):]
            lines = banner.splitlines()
            found_findings = any("FINDINGS" in l for l in lines)
            found_guidance = any("CORRECTIVE GUIDANCE" in l for l in lines)
            found_verify = any("VERIFY" in l for l in lines)
            found_critical = any("[critical]" in l for l in lines)
            found_warning = any("[warning]" in l for l in lines)
            found_note = any("[note]" in l for l in lines)
            self.assertTrue(found_findings, f"FINDINGS missing; banner:\n{banner[:500]}")
            self.assertTrue(found_guidance, "CORRECTIVE GUIDANCE missing")
            self.assertTrue(found_verify, "VERIFY missing")
            self.assertTrue(found_critical, "[critical] missing")
            self.assertTrue(found_warning, "[warning] missing")
            self.assertTrue(found_note, "[note] missing")
        finally:
            self._stop_watcher(proc)

    # ------------------------------------------------------------------
    # 6.7 non-TTY output contains no non-ASCII box-drawing characters
    def test_non_tty_output_no_box_drawing_chars(self) -> None:
        log = self.log_dir / "chg.implement.r1.1.log"
        log.write_text(
            "# --- OPSX WORKER INPUT ---\n"
            "# CHANGE: chg\n"
            "# ROUND: 1\n"
            "# STATE_FILE: /tmp/s\n"
            "# LATEST_FIX_PROMPT: none\n"
            "# TASK_COUNTS: 0/10\n"
            "# CONTEXT_CACHE_STATUS: valid\n"
            "# CONTEXT_CACHE_VALID: true\n"
            "# CONTEXT_CACHE_SUMMARY: summary\n"
            "# --- END OPSX WORKER INPUT ---\n"
            "worker output\n"
        )
        self.time.sleep(0.1)

        proc = self._start_watcher()
        try:
            out = self._read_until(proc, "Stage:", 10.0)
            for ch in ("┌", "┐", "└", "┘", "├", "┤", "│", "─", "╔", "╗", "╚", "╝", "║", "═"):
                self.assertNotIn(
                    ch,
                    out,
                    f"non-ASCII box-drawing char {ch!r} found in non-TTY output",
                )
        finally:
            self._stop_watcher(proc)

    # ------------------------------------------------------------------
    # 6.8 narrow COLUMNS uses plain prefixed form
    def test_narrow_columns_plain_prefixed_form(self) -> None:
        log = self.log_dir / "chg.implement.r1.1.log"
        log.write_text(
            "# --- OPSX WORKER INPUT ---\n"
            "# CHANGE: chg\n"
            "# ROUND: 1\n"
            "# STATE_FILE: /tmp/s\n"
            "# LATEST_FIX_PROMPT: none\n"
            "# TASK_COUNTS: 0/10\n"
            "# CONTEXT_CACHE_STATUS: valid\n"
            "# CONTEXT_CACHE_VALID: true\n"
            "# CONTEXT_CACHE_SUMMARY: summary\n"
            "# --- END OPSX WORKER INPUT ---\n"
            "worker output\n"
        )
        self.time.sleep(0.1)

        env = os.environ.copy()
        env["COLUMNS"] = "30"
        proc = subprocess.Popen(
            ["bash", str(self.watcher_script), str(self.repo)],
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            env=env,
        )
        try:
            out = self._read_until(proc, "Stage:", 10.0)
            self.assertIn("Stage:", out)
            self.assertIn("chg", out)
            self.assertIn("---", out, "plain prefixed form must have --- delimiters")
            self.assertNotIn("┌", out, "box-drawing chars must not appear in narrow mode")
        finally:
            self._stop_watcher(proc)

    # ------------------------------------------------------------------
    # 6.9 ANSI-escaped log lines survive the reader byte-for-byte
    def test_ansi_escaped_lines_survive(self) -> None:
        ansi_line = "\033[32mgreen text\033[0m \033[1mbold\033[0m\n"
        log = self.log_dir / "chg.implement.r1.1.log"
        log.write_text(ansi_line)
        self.time.sleep(0.1)

        proc = self._start_watcher()
        try:
            out = self._read_until(proc, "green text", 10.0)
            self.assertIn("\033[32m", out, "ANSI escape \\033[32m must survive")
            self.assertIn("\033[0m", out, "ANSI escape \\033[0m must survive")
            self.assertIn("\033[1m", out, "ANSI escape \\033[1m must survive")
            self.assertIn("green text", out)
            self.assertIn("bold", out)
        finally:
            self._stop_watcher(proc)

    # ------------------------------------------------------------------
    # Regression: log switch kills the previous tail follower.
    # Without the explicit-PID cleanup (the old `kill %1` only signalled the
    # subshell wrapper, never the tail inside the pipeline), appending to a
    # stale log after a switch leaks output into the watcher stream.
    def test_switch_kills_previous_tail_no_orphan_leak(self) -> None:
        """After switching from log1 to log2, appends to log1 must not appear
        in watcher output — the old tail must be dead."""
        log1 = self.log_dir / "chg.implement.r1.1.log"
        log1.write_text("log1 first line\n")
        self.time.sleep(0.1)

        proc = self._start_watcher()
        try:
            out = self._read_until(proc, "log1 first line", 10.0)
            self.assertIn("log1 first line", out)

            log2 = self.log_dir / "chg.implement.r2.1.log"
            log2.write_text("log2 first line\n")
            out2 = self._read_until(proc, "log2 first line", 10.0)
            self.assertIn("log2 first line", out2,
                          f"watcher must switch to log2; got: {out2[-400:]}")

            # Give the watcher time to settle after switching.
            self.time.sleep(3.0)

            stale_marker = "STALE_LEAK_AFTER_SWITCH_XYZZY"
            with open(str(log1), "a") as f:
                f.write(f"{stale_marker}\n")
                f.flush()

            # Read watcher output for a bit — the stale marker must NOT appear.
            post = self._read_until(proc, stale_marker, 5.0)
            self.assertNotIn(
                stale_marker,
                post,
                f"stale log1 append must not leak into watcher after switching to log2; "
                f"old tail was not killed; output:\n{post[-600:]}",
            )
        finally:
            self._stop_watcher(proc)

    # ------------------------------------------------------------------
    # 6.10 malformed reviewer-like line is still emitted and watcher keeps following
    def test_malformed_reviewer_line_emitted_and_continues(self) -> None:
        log = self.log_dir / "chg.review.r1.1.log"
        log.write_text("review log starts\n")
        self.time.sleep(0.1)

        proc = self._start_watcher()
        try:
            out = self._read_until(proc, "review log starts", 10.0)

            malformed = '{"status":"reviewed","verdict":broken,"finding_counts":{"critical":1}}\n'
            with open(str(log), "a") as f:
                f.write(malformed)

            out2 = self._read_until(proc, "broken", 10.0)
            self.assertIn("broken", out2, "malformed line must still be emitted")

            follow_up = "later worker output\n"
            with open(str(log), "a") as f:
                f.write(follow_up)
                f.flush()

            out3 = self._read_until(proc, "later worker output", 10.0)
            self.assertIn("later worker output", out3,
                          "watcher must continue following after malformed line")
        finally:
            self._stop_watcher(proc)


if __name__ == "__main__":
    unittest.main()
