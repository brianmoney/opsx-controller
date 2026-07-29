"""Verify that all global installers deploy the shared orchestrator runtime.

Each test uses a temporary HOME directory so the real user environment is
never modified.  The assertions cover:

* executables: ``opsx-plan`` and ``opsx-run`` in ``~/.local/bin``
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
        """Verify opsx-plan and opsx-run are present and executable."""
        opsx_plan = self._bin_dir() / "opsx-plan"
        opsx_run = self._bin_dir() / "opsx-run"
        self.assertTrue(opsx_plan.is_file(),
                        f"opsx-plan missing in {self._bin_dir()}")
        self.assertTrue(opsx_run.is_file(),
                        f"opsx-run missing in {self._bin_dir()}")
        self.assertTrue(os.access(str(opsx_plan), os.X_OK))
        self.assertTrue(os.access(str(opsx_run), os.X_OK))

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


if __name__ == "__main__":
    unittest.main()
