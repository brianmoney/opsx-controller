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
import shutil
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
_DSH_INSTALLER = _REPO / "adapters" / "dsh" / "install.sh"
_UNIVERSAL_INSTALLER = _REPO / "install.sh"


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


def _run_installer_project(
    installer: Path, project: Path, home: Path, env: dict[str, str]
) -> subprocess.CompletedProcess:
    """Run a project installer pointing at *project* with *home* as HOME.

    Returns the ``CompletedProcess`` so callers can inspect stdout/stderr.
    """
    return subprocess.run(
        ["bash", str(installer), "--project", str(project)],
        cwd=_REPO,
        env={**os.environ, **env},
        check=True,
        capture_output=True,
        text=True,
    )


def _run_installer_verify(
    installer: Path, home: Path, env: dict[str, str]
) -> subprocess.CompletedProcess:
    """Run a global installer with --verify.

    Returns the ``CompletedProcess`` so callers can inspect stdout and
    return code (verification failures produce non-zero exits).
    """
    return subprocess.run(
        ["bash", str(installer), "--global", "--verify"],
        cwd=_REPO,
        env={**os.environ, **env},
        capture_output=True,
        text=True,
    )


def _run_universal(
    home: Path, env: dict[str, str], *args: str
) -> subprocess.CompletedProcess:
    """Run the repo-root universal installer against *home*.

    *env* must include ``HOME`` (pointing at *home*) plus the
    ``OPSX_*_MODEL`` overrides. Returns the ``CompletedProcess`` so callers
    can inspect stdout/stderr and the return code (delegated adapter
    failures and usage errors produce non-zero exits).
    """
    return subprocess.run(
        ["bash", str(_UNIVERSAL_INSTALLER), *args],
        cwd=_REPO,
        env={**os.environ, **env},
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
        self.assertTrue(self.lib_dir.joinpath("orchestrator").is_dir())

    def test_helper_deploys_orchestrator_package_matching_repo(self) -> None:
        """The installed lib.orchestrator tree matches the repo copy byte-for-byte."""
        self._run_helper()
        installed_pkg = self.lib_dir / "orchestrator"
        repo_pkg = _REPO / "lib" / "orchestrator"
        repo_files = sorted(p.relative_to(repo_pkg) for p in repo_pkg.rglob("*.py"))
        self.assertTrue(repo_files, "expected lib/orchestrator to contain .py modules")
        for rel in repo_files:
            installed_file = installed_pkg / rel
            self.assertTrue(installed_file.is_file(), f"missing installed module: {rel}")
            self.assertEqual(
                hashlib.sha256((repo_pkg / rel).read_bytes()).digest(),
                hashlib.sha256(installed_file.read_bytes()).digest(),
                f"installed module differs from repo copy: {rel}",
            )

    def test_repeated_install_removes_deleted_orchestrator_module(self) -> None:
        """A module present in a prior install but deleted from the repo
        must not persist in the installed copy after a repeat install."""
        self._run_helper()
        installed_pkg = self.lib_dir / "orchestrator"
        stale_module = installed_pkg / "_no_longer_in_repo.py"
        stale_module.write_text("# stale leftover module\n", encoding="utf-8")
        self.assertTrue(stale_module.is_file())

        self._run_helper()
        self.assertFalse(
            stale_module.is_file(),
            "repeated install must replace the managed orchestrator package, "
            "not merge into it",
        )

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

    def test_helper_deploys_canonical_sample_pair(self) -> None:
        self._run_helper()
        samples = self.lib_dir.parent / "samples"
        for name in ("sample-plan.md", "sample-plan.toml"):
            installed = samples / name
            source = _REPO / "orchestrator" / "samples" / name
            self.assertTrue(installed.is_file(), f"sample missing: {installed}")
            self.assertEqual(source.read_bytes(), installed.read_bytes())

    def test_repeated_install_refreshes_canonical_sample_pair(self) -> None:
        self._run_helper()
        samples = self.lib_dir.parent / "samples"
        stale = samples / "sample-plan.md"
        stale.write_text("stale sample\n", encoding="utf-8")

        self._run_helper()

        source = _REPO / "orchestrator" / "samples" / "sample-plan.md"
        self.assertEqual(source.read_bytes(), stale.read_bytes())


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
        for pkg in ("metrics", "pricing", "models", "orchestrator"):
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

    # -- plan-authoring reference global install assertions -------------------

    def _assert_plan_authoring_reference_in_shared_lib(self) -> None:
        """The shared orchestrator must deploy the reference to
        ``~/.local/lib/opsx-controller/plan-authoring.md``."""
        ref = (
            Path(self.home.name) / ".local" / "lib"
            / "opsx-controller" / "plan-authoring.md"
        )
        self.assertTrue(ref.is_file(),
                        f"shared plan-authoring reference missing at {ref}")
        source = _REPO / "core" / "plan-authoring.md"
        self.assertEqual(
            hashlib.sha256(source.read_bytes()).digest(),
            hashlib.sha256(ref.read_bytes()).digest(),
            f"shared plan-authoring reference content differs from source",
        )

    def _assert_opencode_support_has_reference(self) -> None:
        ref = Path(self.home.name) / ".config" / "opencode" / "opsx-controller" / "plan-authoring.md"
        self.assertTrue(ref.is_file(),
                        f"OpenCode plan-authoring reference missing at {ref}")
        source = _REPO / "core" / "plan-authoring.md"
        self.assertEqual(
            hashlib.sha256(source.read_bytes()).digest(),
            hashlib.sha256(ref.read_bytes()).digest(),
        )

    def _assert_claude_support_has_reference(self) -> None:
        ref = Path(self.home.name) / ".claude" / "opsx-controller" / "plan-authoring.md"
        self.assertTrue(ref.is_file(),
                        f"Claude Code plan-authoring reference missing at {ref}")
        source = _REPO / "core" / "plan-authoring.md"
        self.assertEqual(
            hashlib.sha256(source.read_bytes()).digest(),
            hashlib.sha256(ref.read_bytes()).digest(),
        )

    def _assert_codex_support_has_reference(self) -> None:
        ref = Path(self.home.name) / ".codex" / "opsx-controller" / "plan-authoring.md"
        self.assertTrue(ref.is_file(),
                        f"Codex CLI plan-authoring reference missing at {ref}")
        source = _REPO / "core" / "plan-authoring.md"
        self.assertEqual(
            hashlib.sha256(source.read_bytes()).digest(),
            hashlib.sha256(ref.read_bytes()).digest(),
        )

    def test_opencode_global_deploys_plan_authoring_reference(self) -> None:
        _run_installer(_OPENCODE_INSTALLER, Path(self.home.name), self.env)
        self._assert_plan_authoring_reference_in_shared_lib()
        self._assert_opencode_support_has_reference()

    def test_claude_global_deploys_plan_authoring_reference(self) -> None:
        _run_installer(_CLAUDE_INSTALLER, Path(self.home.name), self.env)
        self._assert_plan_authoring_reference_in_shared_lib()
        self._assert_claude_support_has_reference()

    def test_codex_global_deploys_plan_authoring_reference(self) -> None:
        _run_installer(_CODEX_INSTALLER, Path(self.home.name), self.env)
        self._assert_plan_authoring_reference_in_shared_lib()
        self._assert_codex_support_has_reference()

    def test_opencode_global_output_mentions_reference(self) -> None:
        proc = subprocess.run(
            ["bash", str(_OPENCODE_INSTALLER), "--global"],
            cwd=_REPO,
            env={**os.environ, **self.env},
            check=True,
            capture_output=True,
            text=True,
        )
        output = proc.stdout + proc.stderr
        self.assertIn("plan-authoring reference", output,
                      "OpenCode installer output must mention the plan-authoring reference")

    def test_claude_global_output_mentions_reference(self) -> None:
        proc = subprocess.run(
            ["bash", str(_CLAUDE_INSTALLER), "--global"],
            cwd=_REPO,
            env={**os.environ, **self.env},
            check=True,
            capture_output=True,
            text=True,
        )
        output = proc.stdout + proc.stderr
        self.assertIn("plan-authoring reference", output,
                      "Claude Code installer output must mention the plan-authoring reference")

    def test_codex_global_output_mentions_reference(self) -> None:
        proc = subprocess.run(
            ["bash", str(_CODEX_INSTALLER), "--global"],
            cwd=_REPO,
            env={**os.environ, **self.env},
            check=True,
            capture_output=True,
            text=True,
        )
        output = proc.stdout + proc.stderr
        self.assertIn("plan-authoring reference", output,
                      "Codex CLI installer output must mention the plan-authoring reference")

    def test_opencode_verify_succeeds_with_reference(self) -> None:
        _run_installer(_OPENCODE_INSTALLER, Path(self.home.name), self.env)
        proc = _run_installer_verify(_OPENCODE_INSTALLER, Path(self.home.name), self.env)
        self.assertEqual(proc.returncode, 0,
                         f"OpenCode verify must succeed with reference deployed: {proc.stderr}")
        self.assertIn("plan-authoring reference deployed and matches source", proc.stdout)

    def test_claude_verify_succeeds_with_reference(self) -> None:
        _run_installer(_CLAUDE_INSTALLER, Path(self.home.name), self.env)
        proc = _run_installer_verify(_CLAUDE_INSTALLER, Path(self.home.name), self.env)
        self.assertEqual(proc.returncode, 0,
                         f"Claude Code verify must succeed with reference deployed: {proc.stderr}")
        self.assertIn("plan-authoring reference deployed and matches source", proc.stdout)

    def test_codex_verify_succeeds_with_reference(self) -> None:
        _run_installer(_CODEX_INSTALLER, Path(self.home.name), self.env)
        proc = _run_installer_verify(_CODEX_INSTALLER, Path(self.home.name), self.env)
        self.assertEqual(proc.returncode, 0,
                         f"Codex CLI verify must succeed with reference deployed: {proc.stderr}")
        self.assertIn("plan-authoring reference deployed and matches source", proc.stdout)

    # -- dsh adapter cases ------------------------------------------------

    def test_dsh_global_install_deploys_runtime(self) -> None:
        _run_installer(_DSH_INSTALLER, Path(self.home.name), self.env)
        self._assert_executables_installed()
        self._assert_runtime_libraries_installed()

    def test_dsh_global_install_deploys_shim_and_role_files(self) -> None:
        """The dsh global installer must deploy the worker shim to
        ~/.local/bin and the three role instruction files plus support files
        to the global dsh controller support directory."""
        _run_installer(_DSH_INSTALLER, Path(self.home.name), self.env)

        shim = self._bin_dir() / "opsx-dsh-worker"
        self.assertTrue(shim.is_file(), f"dsh worker shim missing at {shim}")
        self.assertTrue(os.access(str(shim), os.X_OK))

        support = Path(self.home.name) / ".config" / "opsx-controller" / "dsh"
        agents = support / "agents"
        for role in ("implementer", "reviewer", "archiver"):
            self.assertTrue(
                (agents / f"opsx-{role}.md").is_file(),
                f"role instruction file opsx-{role}.md missing from {agents}",
            )
        self.assertTrue((support / "README.md").is_file())
        self.assertTrue((support / "plan-authoring.md").is_file())

    def test_dsh_global_install_deploys_plan_authoring_reference(self) -> None:
        _run_installer(_DSH_INSTALLER, Path(self.home.name), self.env)
        self._assert_plan_authoring_reference_in_shared_lib()
        self._assert_dsh_support_has_reference()

    def _assert_dsh_support_has_reference(self) -> None:
        ref = (
            Path(self.home.name) / ".config" / "opsx-controller" / "dsh"
            / "plan-authoring.md"
        )
        self.assertTrue(ref.is_file(),
                        f"dsh plan-authoring reference missing at {ref}")
        source = _REPO / "core" / "plan-authoring.md"
        self.assertEqual(
            hashlib.sha256(source.read_bytes()).digest(),
            hashlib.sha256(ref.read_bytes()).digest(),
        )

    def test_dsh_global_output_mentions_reference(self) -> None:
        proc = subprocess.run(
            ["bash", str(_DSH_INSTALLER), "--global"],
            cwd=_REPO,
            env={**os.environ, **self.env},
            check=True,
            capture_output=True,
            text=True,
        )
        output = proc.stdout + proc.stderr
        self.assertIn("plan-authoring reference", output,
                      "dsh installer output must mention the plan-authoring reference")

    def test_dsh_verify_succeeds_with_reference(self) -> None:
        _run_installer(_DSH_INSTALLER, Path(self.home.name), self.env)
        proc = _run_installer_verify(_DSH_INSTALLER, Path(self.home.name), self.env)
        self.assertEqual(proc.returncode, 0,
                         f"dsh verify must succeed with reference deployed: {proc.stderr}")
        self.assertIn("plan-authoring reference deployed and matches source", proc.stdout)

    def test_dsh_global_output_mentions_shim(self) -> None:
        proc = subprocess.run(
            ["bash", str(_DSH_INSTALLER), "--global"],
            cwd=_REPO,
            env={**os.environ, **self.env},
            check=True,
            capture_output=True,
            text=True,
        )
        self.assertIn("opsx-dsh-worker", proc.stdout + proc.stderr)

    def test_dsh_installer_warns_on_node_without_typescript_type_stripping(self) -> None:
        """A host whose node reports process.features.typescript falsy must be
        warned (not failed) by the dsh installer."""
        fake_node_dir = Path(self.home.name) / "fakenode"
        fake_node_dir.mkdir(parents=True)
        node_shim = fake_node_dir / "node"
        node_shim.write_text(
            "#!/bin/sh\n"
            'if [ "$1" = "-e" ]; then\n'
            "  exit 1\n"
            "fi\n"
            "exit 0\n",
            encoding="utf-8",
        )
        os.chmod(node_shim, 0o755)

        env = {
            **self.env,
            "PATH": str(fake_node_dir) + ":" + os.environ.get("PATH", ""),
        }
        proc = subprocess.run(
            ["bash", str(_DSH_INSTALLER), "--global"],
            cwd=_REPO,
            env=env,
            check=True,
            capture_output=True,
            text=True,
        )
        output = proc.stdout + proc.stderr
        self.assertIn("Warning", output)
        self.assertIn("process.features.typescript", output)
        self.assertIn("type-stripping", output)

    def test_dsh_installer_stays_quiet_when_node_type_stripping_present(self) -> None:
        """A host whose node reports process.features.typescript truthy must
        not receive the type-stripping warning."""
        fake_node_dir = Path(self.home.name) / "fakenode"
        fake_node_dir.mkdir(parents=True)
        node_shim = fake_node_dir / "node"
        node_shim.write_text(
            "#!/bin/sh\n"
            'if [ "$1" = "-e" ]; then\n'
            "  exit 0\n"
            "fi\n"
            "exit 0\n",
            encoding="utf-8",
        )
        os.chmod(node_shim, 0o755)

        env = {
            **self.env,
            "PATH": str(fake_node_dir) + ":" + os.environ.get("PATH", ""),
        }
        proc = subprocess.run(
            ["bash", str(_DSH_INSTALLER), "--global"],
            cwd=_REPO,
            env=env,
            check=True,
            capture_output=True,
            text=True,
        )
        output = proc.stdout + proc.stderr
        self.assertNotIn("process.features.typescript", output)

    def test_dsh_install_usage_error_without_mode(self) -> None:
        """No mode must print a usage message and exit non-zero."""
        proc = subprocess.run(
            ["bash", str(_DSH_INSTALLER)],
            cwd=_REPO,
            env={**os.environ, **self.env},
            capture_output=True,
            text=True,
        )
        self.assertNotEqual(proc.returncode, 0)
        self.assertIn("Usage:", proc.stdout + proc.stderr)

    def test_repeat_install_replaces_stale_reference_opencode(self) -> None:
        """A modified reference must be replaced by the source on repeat install."""
        home = Path(self.home.name)
        support_dir = home / ".config" / "opencode" / "opsx-controller"
        support_dir.mkdir(parents=True)
        (support_dir / "plan-authoring.md").write_text("stale content\n", encoding="utf-8")

        _run_installer(_OPENCODE_INSTALLER, home, self.env)

        installed = support_dir / "plan-authoring.md"
        source = _REPO / "core" / "plan-authoring.md"
        self.assertEqual(
            hashlib.sha256(source.read_bytes()).digest(),
            hashlib.sha256(installed.read_bytes()).digest(),
            "repeat install must replace stale plan-authoring reference",
        )

    def test_repeat_install_replaces_stale_reference_claude(self) -> None:
        home = Path(self.home.name)
        support_dir = home / ".claude" / "opsx-controller"
        support_dir.mkdir(parents=True)
        (support_dir / "plan-authoring.md").write_text("stale content\n", encoding="utf-8")

        _run_installer(_CLAUDE_INSTALLER, home, self.env)

        installed = support_dir / "plan-authoring.md"
        source = _REPO / "core" / "plan-authoring.md"
        self.assertEqual(
            hashlib.sha256(source.read_bytes()).digest(),
            hashlib.sha256(installed.read_bytes()).digest(),
            "repeat install must replace stale plan-authoring reference",
        )

    def test_repeat_install_replaces_stale_reference_codex(self) -> None:
        home = Path(self.home.name)
        support_dir = home / ".codex" / "opsx-controller"
        support_dir.mkdir(parents=True)
        (support_dir / "plan-authoring.md").write_text("stale content\n", encoding="utf-8")

        _run_installer(_CODEX_INSTALLER, home, self.env)

        installed = support_dir / "plan-authoring.md"
        source = _REPO / "core" / "plan-authoring.md"
        self.assertEqual(
            hashlib.sha256(source.read_bytes()).digest(),
            hashlib.sha256(installed.read_bytes()).digest(),
            "repeat install must replace stale plan-authoring reference",
        )

    def test_repeat_install_replaces_stale_reference_dsh(self) -> None:
        home = Path(self.home.name)
        support_dir = home / ".config" / "opsx-controller" / "dsh"
        support_dir.mkdir(parents=True)
        (support_dir / "plan-authoring.md").write_text("stale content\n", encoding="utf-8")

        _run_installer(_DSH_INSTALLER, home, self.env)

        installed = support_dir / "plan-authoring.md"
        source = _REPO / "core" / "plan-authoring.md"
        self.assertEqual(
            hashlib.sha256(source.read_bytes()).digest(),
            hashlib.sha256(installed.read_bytes()).digest(),
            "repeat install must replace stale plan-authoring reference",
        )

    def test_verify_reports_stale_reference_divergence_opencode(self) -> None:
        """After install, verify must detect a reference that diverged post-install
        and report it when the installer is re-run (which replaces and reports)."""
        home = Path(self.home.name)
        _run_installer(_OPENCODE_INSTALLER, home, self.env)

        # Corrupt the installed reference post-install
        support_dir = home / ".config" / "opencode" / "opsx-controller"
        (support_dir / "plan-authoring.md").write_text("post-install corruption\n", encoding="utf-8")

        # Re-install: must replace the corrupted reference and succeed
        proc = _run_installer_verify(_OPENCODE_INSTALLER, home, self.env)
        self.assertEqual(proc.returncode, 0,
                         f"OpenCode verify must succeed after reinstall fixes stale reference: {proc.stderr}")
        self.assertIn("plan-authoring reference deployed and matches source", proc.stdout)

        # Confirm the reference was actually replaced
        source = _REPO / "core" / "plan-authoring.md"
        installed = support_dir / "plan-authoring.md"
        self.assertEqual(
            hashlib.sha256(source.read_bytes()).digest(),
            hashlib.sha256(installed.read_bytes()).digest(),
            "reinstall must replace post-install-corrupted reference",
        )

    def test_verify_reports_stale_reference_divergence_claude(self) -> None:
        home = Path(self.home.name)
        _run_installer(_CLAUDE_INSTALLER, home, self.env)
        support_dir = home / ".claude" / "opsx-controller"
        (support_dir / "plan-authoring.md").write_text("post-install corruption\n", encoding="utf-8")
        proc = _run_installer_verify(_CLAUDE_INSTALLER, home, self.env)
        self.assertEqual(proc.returncode, 0,
                         f"Claude Code verify must succeed after reinstall: {proc.stderr}")
        self.assertIn("plan-authoring reference deployed and matches source", proc.stdout)

    def test_verify_reports_stale_reference_divergence_codex(self) -> None:
        home = Path(self.home.name)
        _run_installer(_CODEX_INSTALLER, home, self.env)
        support_dir = home / ".codex" / "opsx-controller"
        (support_dir / "plan-authoring.md").write_text("post-install corruption\n", encoding="utf-8")
        proc = _run_installer_verify(_CODEX_INSTALLER, home, self.env)
        self.assertEqual(proc.returncode, 0,
                         f"Codex CLI verify must succeed after reinstall: {proc.stderr}")
        self.assertIn("plan-authoring reference deployed and matches source", proc.stdout)

    def test_verify_reports_stale_reference_divergence_dsh(self) -> None:
        home = Path(self.home.name)
        _run_installer(_DSH_INSTALLER, home, self.env)
        support_dir = home / ".config" / "opsx-controller" / "dsh"
        (support_dir / "plan-authoring.md").write_text("post-install corruption\n", encoding="utf-8")
        proc = _run_installer_verify(_DSH_INSTALLER, home, self.env)
        self.assertEqual(proc.returncode, 0,
                         f"dsh verify must succeed after reinstall: {proc.stderr}")
        self.assertIn("plan-authoring reference deployed and matches source", proc.stdout)

    def test_opencode_install_removes_stale_legacy_commands(self) -> None:
        """Global reinstall must remove previously-deployed legacy command
        files that are no longer shipped."""
        home = Path(self.home.name)
        cmds = home / ".config" / "opencode" / "commands"
        cmds.mkdir(parents=True)
        for legacy in ("opsx-author.md", "opsx-archive-no-prompt.md",
                       "opsx-verify-auto.md", "opsx-review.md",
                       "opsx-drive.md"):
            (cmds / legacy).write_text("stale legacy command\n", encoding="utf-8")

        _run_installer(_OPENCODE_INSTALLER, home, self.env)

        for legacy in ("opsx-author.md", "opsx-archive-no-prompt.md",
                       "opsx-verify-auto.md", "opsx-review.md",
                       "opsx-drive.md"):
            self.assertFalse(
                (cmds / legacy).exists(),
                f"stale legacy command {legacy} must be removed by installer",
            )
        self.assertTrue(
            (cmds / "opsx-plan.md").is_file(),
            "supported command opsx-plan must survive",
        )

    def test_opencode_install_removes_stale_nested_agent(self) -> None:
        """Global reinstall must remove the previously-deployed nested
        opsx-controller agent."""
        home = Path(self.home.name)
        agents = home / ".config" / "opencode" / "agents"
        agents.mkdir(parents=True)
        (agents / "opsx-controller.md").write_text("stale\n", encoding="utf-8")

        _run_installer(_OPENCODE_INSTALLER, home, self.env)

        self.assertFalse(
            (agents / "opsx-controller.md").exists(),
            "stale nested controller agent must be removed",
        )
        self.assertTrue(
            (agents / "opsx-implementer.md").is_file(),
            "supported worker agent must survive",
        )

    def test_opencode_agent_variant_defaults_when_unset(self) -> None:
        """With no OPSX_<ROLE>_VARIANT configured, installed agents carry the
        built-in defaults (reviewer: xhigh, others: high)."""
        home = Path(self.home.name)
        _run_installer(_OPENCODE_INSTALLER, home, self.env)
        agents = home / ".config" / "opencode" / "agents"
        reviewer = (agents / "opsx-reviewer.md").read_text(encoding="utf-8")
        implementer = (agents / "opsx-implementer.md").read_text(encoding="utf-8")
        archiver = (agents / "opsx-archiver.md").read_text(encoding="utf-8")
        self.assertIn('variant: "xhigh"', reviewer)
        self.assertIn('variant: "high"', implementer)
        self.assertIn('variant: "high"', archiver)
        for name, text in (("reviewer", reviewer), ("implementer", implementer), ("archiver", archiver)):
            self.assertNotIn("{env:", text, f"unsubstituted placeholder left in {name}")

    def test_opencode_agent_variant_override_from_env(self) -> None:
        """An OPSX_<ROLE>_VARIANT override lands in the installed agent
        frontmatter (e.g. models whose effort labels exclude xhigh)."""
        home = Path(self.home.name)
        env = {**self.env, "OPSX_REVIEWER_VARIANT": "max"}
        _run_installer(_OPENCODE_INSTALLER, home, env)
        agents = home / ".config" / "opencode" / "agents"
        reviewer = (agents / "opsx-reviewer.md").read_text(encoding="utf-8")
        implementer = (agents / "opsx-implementer.md").read_text(encoding="utf-8")
        self.assertIn('variant: "max"', reviewer)
        self.assertIn('variant: "high"', implementer)

    def test_claude_install_removes_stale_opsx_drive_skill(self) -> None:
        """Global reinstall must remove previously-deployed opsx-drive skill."""
        home = Path(self.home.name)
        drive_dir = home / ".claude" / "skills" / "opsx-drive"
        drive_dir.mkdir(parents=True)
        (drive_dir / "SKILL.md").write_text("stale skill\n", encoding="utf-8")

        _run_installer(_CLAUDE_INSTALLER, home, self.env)

        self.assertFalse(
            drive_dir.exists(),
            "stale opsx-drive skill must be removed by installer",
        )
        self.assertTrue(
            (home / ".claude" / "skills" / "opsx-plan" / "SKILL.md").is_file(),
            "supported opsx-plan skill must survive",
        )

    def test_codex_install_removes_stale_opsx_drive_skill(self) -> None:
        """Global reinstall must remove previously-deployed opsx-drive skill."""
        home = Path(self.home.name)
        drive_dir = home / ".agents" / "skills" / "opsx-drive"
        drive_dir.mkdir(parents=True)
        (drive_dir / "SKILL.md").write_text("stale\n", encoding="utf-8")

        _run_installer(_CODEX_INSTALLER, home, self.env)

        self.assertFalse(
            drive_dir.exists(),
            "stale opsx-drive skill must be removed by Codex installer",
        )

    def test_codex_plugin_excludes_stale_opsx_drive(self) -> None:
        """Codex --plugin output must not contain opsx-drive."""
        subprocess.run(
            ["bash", str(_CODEX_INSTALLER), "--plugin"],
            cwd=_REPO,
            env={**os.environ, **self.env},
            check=True,
            capture_output=True,
            text=True,
        )
        plugin_dir = _CODEX_INSTALLER.parent / "plugin"
        self.assertFalse(
            (plugin_dir / "skills" / "opsx-drive").exists(),
            "plugin bundle must not contain opsx-drive",
        )
        self.assertTrue(
            (plugin_dir / "agents").is_dir(),
            "plugin bundle must contain agent directory",
        )

    def test_codex_global_install_deploys_opsx_plan_skill(self) -> None:
        """Codex --global must deploy the opsx-plan skill."""
        _run_installer(_CODEX_INSTALLER, Path(self.home.name), self.env)
        skill_path = (
            Path(self.home.name) / ".agents" / "skills" / "opsx-plan" / "SKILL.md"
        )
        self.assertTrue(
            skill_path.is_file(),
            f"opsx-plan skill must exist at {skill_path}",
        )
        content = skill_path.read_text(encoding="utf-8")
        self.assertIn("plan-authoring.md", content,
                       "opsx-plan skill must reference plan-authoring reference")

    def test_codex_plugin_includes_opsx_plan_skill(self) -> None:
        """Codex --plugin must include the opsx-plan skill."""
        subprocess.run(
            ["bash", str(_CODEX_INSTALLER), "--plugin"],
            cwd=_REPO,
            env={**os.environ, **self.env},
            check=True,
            capture_output=True,
            text=True,
        )
        plugin_dir = _CODEX_INSTALLER.parent / "plugin"
        skill_path = plugin_dir / "skills" / "opsx-plan" / "SKILL.md"
        self.assertTrue(
            skill_path.is_file(),
            "plugin bundle must contain opsx-plan skill",
        )
        content = skill_path.read_text(encoding="utf-8")
        self.assertIn("plan-authoring.md", content,
                       "plugin opsx-plan skill must reference plan-authoring reference")
        self.assertIn("unsupported", content,
                       "Codex opsx-plan skill must state plan-run is unsupported")


class ProjectInstallerTests(unittest.TestCase):
    """Verify that project-level installs deploy the plan-authoring reference."""

    def setUp(self) -> None:
        self.home = tempfile.TemporaryDirectory()
        self.project = tempfile.TemporaryDirectory()
        self.env = {**_model_env(), "HOME": self.home.name}

    def tearDown(self) -> None:
        self.home.cleanup()
        self.project.cleanup()

    def _assert_project_reference(self, ref: Path) -> None:
        self.assertTrue(ref.is_file(),
                        f"plan-authoring reference missing at {ref}")
        source = _REPO / "core" / "plan-authoring.md"
        self.assertEqual(
            hashlib.sha256(source.read_bytes()).digest(),
            hashlib.sha256(ref.read_bytes()).digest(),
            f"project reference content differs from source at {ref}",
        )

    def test_opencode_project_deploys_reference(self) -> None:
        proc = _run_installer_project(
            _OPENCODE_INSTALLER, Path(self.project.name), Path(self.home.name),
            self.env,
        )
        ref = (
            Path(self.project.name) / ".opencode" / "opsx-controller"
            / "plan-authoring.md"
        )
        self._assert_project_reference(ref)
        self.assertIn("plan-authoring reference", proc.stdout,
                      "OpenCode project install output must mention the reference")

    def test_claude_project_deploys_reference(self) -> None:
        proc = _run_installer_project(
            _CLAUDE_INSTALLER, Path(self.project.name), Path(self.home.name),
            self.env,
        )
        ref = (
            Path(self.project.name) / ".claude" / "opsx-controller"
            / "plan-authoring.md"
        )
        self._assert_project_reference(ref)
        self.assertIn("plan-authoring reference", proc.stdout,
                      "Claude Code project install output must mention the reference")

    def test_codex_project_deploys_reference(self) -> None:
        proc = _run_installer_project(
            _CODEX_INSTALLER, Path(self.project.name), Path(self.home.name),
            self.env,
        )
        ref = (
            Path(self.project.name) / ".codex" / "opsx-controller"
            / "plan-authoring.md"
        )
        self._assert_project_reference(ref)
        self.assertIn("plan-authoring reference", proc.stdout,
                      "Codex CLI project install output must mention the reference")

    def test_dsh_project_deploys_reference_and_role_files(self) -> None:
        proc = _run_installer_project(
            _DSH_INSTALLER, Path(self.project.name), Path(self.home.name),
            self.env,
        )
        support = (
            Path(self.project.name) / ".opsx-controller" / "dsh"
        )
        ref = support / "plan-authoring.md"
        self._assert_project_reference(ref)
        self.assertIn("plan-authoring reference", proc.stdout,
                      "dsh project install output must mention the reference")
        agents = support / "agents"
        for role in ("implementer", "reviewer", "archiver"):
            self.assertTrue(
                (agents / f"opsx-{role}.md").is_file(),
                f"project role instruction file opsx-{role}.md missing from {agents}",
            )

    def test_dsh_project_install_shadows_global_role_files(self) -> None:
        """A project install must make the installed shim resolve the
        project-installed role files first when run from the project dir."""
        _run_installer(_DSH_INSTALLER, Path(self.home.name), self.env)
        _run_installer_project(
            _DSH_INSTALLER, Path(self.project.name), Path(self.home.name),
            self.env,
        )

        shim = Path(self.home.name) / ".local" / "bin" / "opsx-dsh-worker"
        self.assertTrue(shim.is_file(), f"dsh worker shim missing at {shim}")

        project_role = (
            Path(self.project.name) / ".opsx-controller" / "dsh" / "agents"
            / "opsx-implementer.md"
        )
        global_role = (
            Path(self.home.name) / ".config" / "opsx-controller" / "dsh" / "agents"
            / "opsx-implementer.md"
        )
        self.assertTrue(project_role.is_file(), "project role file missing")
        self.assertTrue(global_role.is_file(), "global role file missing")

        # Make the global copy stale so the two copies are distinguishable:
        # the shim must compose from the project copy, not the stale global.
        stale_marker = "STALE-GLOBAL-IMPLEMENTER-MARKER"
        global_role.write_text(
            f"stale global instructions {stale_marker}\n", encoding="utf-8"
        )
        project_content = project_role.read_text(encoding="utf-8")

        capture = Path(self.project.name) / "dsh-argv-capture.txt"
        fake = Path(self.project.name) / "fake-dsh"
        fake.write_text(
            "#!/bin/sh\nprintf '%s\\n' \"$@\" > \"" + str(capture) + "\"\n",
            encoding="utf-8",
        )
        os.chmod(fake, 0o755)

        proc = subprocess.run(
            [str(shim), "--role", "implementer", "CHANGE: add-example\nROUND: 1\n"],
            cwd=Path(self.project.name),
            env={
                **os.environ,
                **self.env,
                "PATH": "/usr/bin:/bin:/usr/local/bin:" + os.environ.get("PATH", ""),
                "DSH_BINARY": str(fake),
            },
            capture_output=True,
            text=True,
        )
        self.assertEqual(proc.returncode, 0, f"shim failed: {proc.stderr}")
        self.assertTrue(capture.is_file(), "fake dsh never captured argv")
        captured = capture.read_text(encoding="utf-8")
        self.assertNotIn(
            stale_marker,
            captured,
            "shim must resolve the project-installed role file first",
        )
        self.assertIn(project_content.strip(), captured)

    def test_codex_plugin_includes_reference(self) -> None:
        """Codex --plugin output must contain plan-authoring.md."""
        subprocess.run(
            ["bash", str(_CODEX_INSTALLER), "--plugin"],
            cwd=_REPO,
            env={**os.environ, **self.env},
            check=True,
            capture_output=True,
            text=True,
        )
        plugin_dir = _CODEX_INSTALLER.parent / "plugin"
        ref = plugin_dir / "opsx-controller" / "plan-authoring.md"
        self.assertTrue(
            ref.is_file(),
            "plugin bundle must contain plan-authoring.md",
        )
        source = _REPO / "core" / "plan-authoring.md"
        self.assertEqual(
            hashlib.sha256(source.read_bytes()).digest(),
            hashlib.sha256(ref.read_bytes()).digest(),
            "plugin bundle plan-authoring.md content must match source",
        )

    def test_project_repeat_install_replaces_stale_reference(self) -> None:
        """A stale project reference must be replaced on repeat install."""
        support_dir = (
            Path(self.project.name) / ".opencode" / "opsx-controller"
        )
        support_dir.mkdir(parents=True)
        (support_dir / "plan-authoring.md").write_text(
            "stale project content\n", encoding="utf-8"
        )

        _run_installer_project(
            _OPENCODE_INSTALLER, Path(self.project.name), Path(self.home.name),
            self.env,
        )

        installed = support_dir / "plan-authoring.md"
        source = _REPO / "core" / "plan-authoring.md"
        self.assertEqual(
            hashlib.sha256(source.read_bytes()).digest(),
            hashlib.sha256(installed.read_bytes()).digest(),
            "repeat project install must replace stale plan-authoring reference",
        )

    def test_codex_project_deploys_opsx_plan_skill(self) -> None:
        """Codex --project must deploy the opsx-plan skill."""
        proc = _run_installer_project(
            _CODEX_INSTALLER, Path(self.project.name), Path(self.home.name),
            self.env,
        )
        skill_path = (
            Path(self.project.name) / ".agents" / "skills"
            / "opsx-plan" / "SKILL.md"
        )
        self.assertTrue(
            skill_path.is_file(),
            f"project opsx-plan skill must exist at {skill_path}",
        )
        content = skill_path.read_text(encoding="utf-8")
        self.assertIn("plan-authoring.md", content,
                       "project opsx-plan skill must reference plan-authoring reference")
        self.assertIn("skills", proc.stdout,
                       "Codex project install output must mention skills")


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

    def test_stale_detection_after_dsh_install(self) -> None:
        self._install_via(_DSH_INSTALLER)
        ok, label, msg = self._call_stale_check(
            self._load_opsx_plan(), self.home.name
        )
        self.assertTrue(ok, f"stale check failed after dsh install: {msg}")

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

    def test_detects_content_mismatch_after_dsh_install(self) -> None:
        """Modify the installed copy after dsh install and assert mismatch is detected."""
        self._install_via(_DSH_INSTALLER)
        installed = Path(self.home.name) / ".local" / "bin" / "opsx-plan"
        # Corrupt the installed copy
        installed.write_bytes(b"corrupted content")
        ok, label, msg = self._call_stale_check(
            self._load_opsx_plan(), self.home.name
        )
        self.assertFalse(ok, "stale check should detect content mismatch after dsh install")
        self.assertIn("stale", msg.lower())

    def _installed_orchestrator_pkg(self) -> Path:
        return (
            Path(self.home.name) / ".local" / "lib" / "opsx-controller"
            / "lib" / "orchestrator"
        )

    def test_detects_missing_orchestrator_package(self) -> None:
        """An installed runtime with metrics/pricing/models but no
        orchestrator package (predates this layout) must be reported stale,
        not raise, even though the entrypoint itself still matches."""
        import shutil as _shutil

        self._install_via(_OPENCODE_INSTALLER)
        _shutil.rmtree(self._installed_orchestrator_pkg())
        ok, label, msg = self._call_stale_check(
            self._load_opsx_plan(), self.home.name
        )
        self.assertFalse(ok, "stale check should detect a missing orchestrator package")
        self.assertIn("orchestrator", msg.lower())

    def test_detects_stale_orchestrator_module(self) -> None:
        """A single differing module inside the installed lib.orchestrator
        tree must be reported stale even though the entrypoint matches."""
        self._install_via(_OPENCODE_INSTALLER)
        base_py = self._installed_orchestrator_pkg() / "base.py"
        self.assertTrue(base_py.is_file())
        base_py.write_text("# corrupted\n", encoding="utf-8")
        ok, label, msg = self._call_stale_check(
            self._load_opsx_plan(), self.home.name
        )
        self.assertFalse(ok, "stale check should detect a stale orchestrator module")
        self.assertIn("orchestrator", msg.lower())

    def test_detects_orchestrator_module_only_in_installed_copy(self) -> None:
        """A module present only in the installed copy (deleted from the
        repo) must be reported stale."""
        self._install_via(_OPENCODE_INSTALLER)
        extra = self._installed_orchestrator_pkg() / "_extra_not_in_repo.py"
        extra.write_text("# leftover\n", encoding="utf-8")
        ok, label, msg = self._call_stale_check(
            self._load_opsx_plan(), self.home.name
        )
        self.assertFalse(
            ok, "stale check should detect a module only present in the installed copy"
        )
        self.assertIn("orchestrator", msg.lower())


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

    def _start_watcher(self, watcher_script: Path | None = None) -> subprocess.Popen:
        return subprocess.Popen(
            ["bash", str(watcher_script or self.watcher_script), str(self.repo)],
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

    def test_installed_watcher_follows_initial_log_and_switches_to_newer(self) -> None:
        """The installed watcher must behave like the repository watcher."""
        home = tempfile.TemporaryDirectory()
        try:
            subprocess.run(
                ["bash", str(_REPO / "scripts" / "install-orchestrator.sh"), str(_REPO)],
                cwd=_REPO,
                env={**os.environ, "HOME": home.name},
                check=True,
                capture_output=True,
                text=True,
            )
            log1 = self.log_dir / "chg.implement.r1.1.log"
            log1.write_text("installed round 1 output\n")
            self.time.sleep(0.1)

            installed_watcher = Path(home.name) / ".local" / "bin" / "opsx-watch-plan"
            proc = self._start_watcher(installed_watcher)
            try:
                out1 = self._read_until(proc, "installed round 1 output", 10.0)
                self.assertIn("installed round 1 output", out1)
                log2 = self.log_dir / "chg.implement.r2.1.log"
                log2.write_text("installed round 2 output QWERTY\n")
                out2 = self._read_until(proc, "QWERTY", 10.0)
                self.assertIn("QWERTY", out2)
            finally:
                self._stop_watcher(proc)
        finally:
            home.cleanup()

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

    # ------------------------------------------------------------------
    # 6.11 watcher scopes logs to active-plan change ids
    def test_watcher_scopes_logs_to_active_plan_change_ids(self) -> None:
        """When an active plan is resolved, the watcher must only follow logs
        whose change prefix matches a change id in the active plan TOML.
        Logs from changes not in the plan must be ignored."""
        import os as _os

        dot_plan = self.repo / ".opsx-plan"
        dot_plan.mkdir(parents=True, exist_ok=True)
        (dot_plan / ".gitignore").write_text("*\n", encoding="utf-8")

        # Active plan pointer.
        (dot_plan / "active-plan").write_text(
            "openspec/plans/test-plan.toml\n", encoding="utf-8"
        )

        plan_dir = self.repo / "openspec" / "plans"
        plan_dir.mkdir(parents=True)
        plan_toml = plan_dir / "test-plan.toml"
        plan_toml.write_text(
            '[plan]\nname = "test-plan"\nadapter = "opencode"\n\n'
            '[[changes]]\nid = "chg-alpha"\nphase = 1\nenabled = true\n\n'
            '[[changes]]\nid = "chg-beta"\nphase = 2\nenabled = true\n',
            encoding="utf-8",
        )

        in_plan_log = self.log_dir / "chg-alpha.implement.r1.1.log"
        in_plan_log.write_text("in-plan output ZXCVB\n")
        self.time.sleep(0.1)

        out_plan_log = self.log_dir / "chg-other.review.r3.1.log"
        out_plan_log.write_text("out-of-plan output SHOULD_NOT_APPEAR\n")
        self.time.sleep(0.1)

        proc = self._start_watcher()
        try:
            out = self._read_until(proc, "ZXCVB", 10.0)
            self.assertIn(
                "in-plan output ZXCVB",
                out,
                f"watcher must follow in-plan log; got: {out[:400]}",
            )
            self.assertNotIn(
                "SHOULD_NOT_APPEAR",
                out,
                "out-of-plan log must not be followed when active plan is set",
            )
        finally:
            self._stop_watcher(proc)

    def test_watcher_without_active_plan_follows_all_logs(self) -> None:
        """When no active plan is set, the watcher must follow all logs
        (legacy / test behaviour)."""
        log1 = self.log_dir / "chg-x.implement.r1.1.log"
        log1.write_text("log1 output AAA\n")
        self.time.sleep(0.1)

        proc = self._start_watcher()
        try:
            out = self._read_until(proc, "AAA", 10.0)
            self.assertIn("log1 output AAA", out)
        finally:
            self._stop_watcher(proc)


class CleanHomeReportDashboardTests(unittest.TestCase):
    """A global install from a clean HOME must run report/dashboard using
    only the installed lib.orchestrator package, never the repo checkout."""

    def setUp(self) -> None:
        self.home = tempfile.TemporaryDirectory()
        self.target_repo = tempfile.TemporaryDirectory()
        env = {**_model_env(), "HOME": self.home.name}
        _run_installer(_OPENCODE_INSTALLER, Path(self.home.name), env)

        repo = Path(self.target_repo.name)
        subprocess.run(["git", "init"], cwd=repo, check=True, capture_output=True)
        subprocess.run(
            ["git", "-c", "user.email=test@example.invalid",
             "-c", "user.name=Test User", "commit", "-m", "init", "--allow-empty"],
            cwd=repo, check=True, capture_output=True,
        )
        plan_dir = repo / ".opsx-plan"
        plan_dir.mkdir(parents=True)
        (plan_dir / "clean-home-plan.toml").write_text(
            '[plan]\nname = "clean-home-plan"\nadapter = "opencode"\n\n'
            '[[changes]]\nid = "ch-only"\nphase = 1\n',
            encoding="utf-8",
        )
        (plan_dir / "telemetry").mkdir()
        (plan_dir / "telemetry" / "clean-home-plan.jsonl").write_text("", encoding="utf-8")
        (plan_dir / "clean-home-plan.state.json").write_text(
            '{"plan": "clean-home-plan", "approvals": [], '
            '"changes": {"ch-only": {"status": "pending", "round": 0, "phase": "pending"}}}',
            encoding="utf-8",
        )

    def tearDown(self) -> None:
        self.home.cleanup()
        self.target_repo.cleanup()

    def _run_installed(self, *args: str) -> subprocess.CompletedProcess:
        installed = Path(self.home.name) / ".local" / "bin" / "opsx-plan"
        # Clean environment: no PYTHONPATH pointing at the repo checkout,
        # and cwd outside the checkout, so any accidental import of the
        # checkout's lib/ would only succeed via the installer's own
        # sys.path bootstrap of the installed copy.
        env = {k: v for k, v in os.environ.items() if k != "PYTHONPATH"}
        env["HOME"] = self.home.name
        return subprocess.run(
            [str(installed), "--repo", self.target_repo.name, *args],
            cwd=self.home.name,
            env=env,
            capture_output=True,
            text=True,
        )

    def test_installed_report_runs_without_repo_checkout(self) -> None:
        proc = self._run_installed(
            "report", ".opsx-plan/clean-home-plan.toml", "--json"
        )
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertIn("clean-home-plan", proc.stdout)

    def test_installed_dashboard_runs_without_repo_checkout(self) -> None:
        proc = self._run_installed(
            "dashboard", ".opsx-plan/clean-home-plan.toml"
        )
        self.assertEqual(proc.returncode, 0, proc.stderr)
        html_path = (
            Path(self.target_repo.name) / ".opsx-plan" / "dashboards"
            / "clean-home-plan.html"
        )
        self.assertTrue(html_path.is_file())


class UniversalInstallerTests(unittest.TestCase):
    """Exercise the repo-root universal installer.

    The universal installer delegates to each adapter installer, so its
    tests run against a temporary HOME and assert both the adapter-specific
    artifacts and the shared orchestrator executables/runtime. The adapter
    delegation path is exercised by faking one adapter installer so a
    simulated failure does not touch the real adapter installers.
    """

    ADAPTERS = ("opencode", "claude-code", "codex-cli", "dsh")

    def setUp(self) -> None:
        self.home = tempfile.TemporaryDirectory()
        self.env = {**_model_env(), "HOME": self.home.name}
        self.fake_adapters = Path(self.home.name) / "fake-adapters"

    def tearDown(self) -> None:
        self.home.cleanup()

    # -- artifact-path helpers ---------------------------------------------

    def _opencode_artifacts(self) -> list[Path]:
        root = Path(self.home.name) / ".config" / "opencode"
        return [
            root / "skills",
            root / "commands",
            root / "agents",
            root / "plugins",
            root / "opsx-controller",
        ]

    def _claude_artifacts(self) -> list[Path]:
        return [
            Path(self.home.name) / ".claude" / "skills",
            Path(self.home.name) / ".claude" / "agents",
            Path(self.home.name) / ".claude" / "opsx-controller",
        ]

    def _codex_artifacts(self) -> list[Path]:
        return [
            Path(self.home.name) / ".agents" / "skills",
            Path(self.home.name) / ".codex" / "agents",
            Path(self.home.name) / ".codex" / "opsx-controller",
        ]

    def _dsh_artifacts(self) -> list[Path]:
        root = Path(self.home.name) / ".config" / "opsx-controller" / "dsh"
        return [
            Path(self.home.name) / ".local" / "bin" / "opsx-dsh-worker",
            root / "agents",
            root,
        ]

    def _assert_shared_orchestrator_installed(self) -> None:
        bin_dir = Path(self.home.name) / ".local" / "bin"
        for name in ("opsx-plan", "opsx-run", "opsx-watch-plan"):
            exe = bin_dir / name
            self.assertTrue(exe.is_file(), f"shared executable missing at {exe}")
            self.assertTrue(os.access(str(exe), os.X_OK))
        lib = Path(self.home.name) / ".local" / "lib" / "opsx-controller" / "lib"
        for pkg in ("metrics", "pricing", "models", "orchestrator"):
            self.assertTrue(lib.joinpath(pkg).is_dir(),
                            f"runtime library '{pkg}' missing in {lib}")

    def _adapter_artifact_map(self) -> dict[str, list[Path]]:
        return {
            "opencode": self._opencode_artifacts(),
            "claude-code": self._claude_artifacts(),
            "codex-cli": self._codex_artifacts(),
            "dsh": self._dsh_artifacts(),
        }

    def _assert_adapter_installed(self, adapter: str) -> None:
        for artifact in self._adapter_artifact_map()[adapter]:
            self.assertTrue(artifact.exists(),
                            f"{adapter} artifact missing at {artifact}")

    def _assert_adapter_not_installed(self, adapter: str) -> None:
        for artifact in self._adapter_artifact_map()[adapter]:
            self.assertFalse(artifact.exists(),
                             f"{adapter} artifact unexpectedly present at {artifact}")

    def _assert_all_adapters_installed(self) -> None:
        for adapter in self.ADAPTERS:
            self._assert_adapter_installed(adapter)

    def _install(self, *args: str) -> subprocess.CompletedProcess:
        return _run_universal(Path(self.home.name), self.env, *args)

    # -- fake adapter installer helpers ------------------------------------

    def _write_fake_adapter(self, name: str, exit_code: int = 0) -> Path:
        """Install a fake ``adapters/<name>/install.sh`` in the repo mirror
        that records the arguments it was invoked with and exits with
        *exit_code*."""
        dest = self.fake_adapters / "adapters" / name
        dest.mkdir(parents=True)
        log = self.fake_adapters / f"{name}.invocations"
        script = dest / "install.sh"
        script.write_text(
            "#!/usr/bin/env bash\n"
            f'echo "$@" >> "{log}"\n'
            f'exit {exit_code}\n',
            encoding="utf-8",
        )
        os.chmod(script, 0o755)
        return script

    def _run_universal_with_fake_adapters(
        self, *args: str
    ) -> subprocess.CompletedProcess:
        """Run the universal installer against a repo mirror whose adapter
        delegation resolves to fake adapter installers.

        The mirror is a copy of the real ``install.sh`` plus fake
        ``adapters/<name>/install.sh`` scripts and a symlinked ``lib`` so the
        universal installer's ``install-common.sh`` source resolves. The real
        adapter installers are never touched.
        """
        shutil.copy2(_UNIVERSAL_INSTALLER, self.fake_adapters / "install.sh")
        (self.fake_adapters / "lib").symlink_to(_REPO / "lib", target_is_directory=True)
        return subprocess.run(
            ["bash", str(self.fake_adapters / "install.sh"), *args],
            cwd=_REPO,
            env={**os.environ, **self.env},
            capture_output=True,
            text=True,
        )

    def _invocations(self, name: str) -> list[str]:
        log = self.fake_adapters / f"{name}.invocations"
        if not log.is_file():
            return []
        return [
            line.strip()
            for line in log.read_text(encoding="utf-8").splitlines()
            if line.strip()
        ]

    # -- tests -------------------------------------------------------------

    def test_universal_global_installs_every_adapter_and_shared_runtime(self) -> None:
        proc = self._install("--global")
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self._assert_all_adapters_installed()
        self._assert_shared_orchestrator_installed()
        output = proc.stdout + proc.stderr
        for adapter in self.ADAPTERS:
            self.assertIn(f"[install] {adapter}: OK", output)

    def test_universal_global_only_opencode_installs_just_opencode(self) -> None:
        proc = self._install("--global", "--only", "opencode")
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self._assert_adapter_installed("opencode")
        for adapter in ("claude-code", "codex-cli", "dsh"):
            self._assert_adapter_not_installed(adapter)
        self._assert_shared_orchestrator_installed()
        output = proc.stdout + proc.stderr
        self.assertIn("[install] opencode: OK", output)
        self.assertNotIn("[install] claude-code", output)

    def test_invalid_only_value_exits_nonzero_with_usage(self) -> None:
        proc = self._install("--global", "--only", "bogus")
        self.assertNotEqual(proc.returncode, 0)
        self.assertIn("Error: unknown adapter: bogus", proc.stderr)
        self.assertIn("Valid adapters", proc.stderr)
        for adapter in self.ADAPTERS:
            self.assertIn(adapter, proc.stderr)

    def test_universal_global_help_lists_all_flags(self) -> None:
        proc = self._install("--help")
        self.assertEqual(proc.returncode, 0, proc.stderr)
        for flag in ("--global", "--project", "--verify", "--only"):
            self.assertIn(flag, proc.stdout)

    def test_failed_adapter_reports_partial_failure_and_keeps_prior_artifacts(
        self,
    ) -> None:
        self._write_fake_adapter("opencode", exit_code=0)
        self._write_fake_adapter("claude-code", exit_code=1)
        self._write_fake_adapter("codex-cli", exit_code=0)
        self._write_fake_adapter("dsh", exit_code=0)

        proc = self._run_universal_with_fake_adapters("--global")
        self.assertNotEqual(proc.returncode, 0)
        output = proc.stdout + proc.stderr
        self.assertIn("Completed adapters: opencode", output)
        self.assertIn("Failed adapters:    claude-code", output)
        self.assertNotIn("[install] codex-cli: OK", output)
        self.assertIn("[install] opencode: OK", output)

    def test_verify_flag_passed_through_to_each_adapter(self) -> None:
        self._write_fake_adapter("opencode")
        self._write_fake_adapter("claude-code")
        self._write_fake_adapter("codex-cli")
        self._write_fake_adapter("dsh")

        proc = self._run_universal_with_fake_adapters(
            "--global", "--only", "opencode,claude-code", "--verify"
        )
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertEqual(
            self._invocations("opencode"),
            ["--global --verify"],
            "opencode installer must receive the mode and --verify",
        )
        self.assertEqual(
            self._invocations("claude-code"),
            ["--global --verify"],
            "claude-code installer must receive the mode and --verify",
        )
        self.assertEqual(self._invocations("codex-cli"), [])
        self.assertEqual(self._invocations("dsh"), [])

    def test_verify_flag_runs_each_real_adapter_verification_path(self) -> None:
        proc = self._install("--global", "--verify")
        self.assertEqual(proc.returncode, 0, proc.stderr)
        output = proc.stdout + proc.stderr
        for adapter in self.ADAPTERS:
            self.assertIn(f"[install] {adapter}: OK", output)
        self.assertEqual(output.count("plan-authoring reference deployed and matches source"), 4)


def _run_universal_project(
    home: Path, project: Path, env: dict[str, str], *args: str
) -> subprocess.CompletedProcess:
    """Run the repo-root universal installer with --project against *project*.

    *env* must include ``HOME`` (pointing at *home*) plus the
    ``OPSX_*_MODEL`` overrides. Returns the ``CompletedProcess`` so callers
    can inspect stdout/stderr and the return code.
    """
    return subprocess.run(
        ["bash", str(_UNIVERSAL_INSTALLER), "--project", str(project), *args],
        cwd=_REPO,
        env={**os.environ, **env},
        capture_output=True,
        text=True,
    )


class UniversalProjectInstallTests(unittest.TestCase):
    """Exercise the universal installer's project-scoped shared runtime.

    ``--project`` must install every selected adapter's project artifacts
    plus a self-contained shared orchestrator runtime under
    ``<project>/.opsx-controller`` — executables and the ``metrics``,
    ``pricing``, ``models``, and ``orchestrator`` runtime packages — that
    runs without importing anything from the repository checkout.
    """

    ADAPTERS = ("opencode", "claude-code", "codex-cli", "dsh")

    def setUp(self) -> None:
        self.home = tempfile.TemporaryDirectory()
        self.project = tempfile.TemporaryDirectory()
        self.env = {**_model_env(), "HOME": self.home.name}

    def tearDown(self) -> None:
        self.home.cleanup()
        self.project.cleanup()

    def _project_root(self) -> Path:
        return Path(self.project.name)

    def _runtime_dir(self) -> Path:
        return self._project_root() / ".opsx-controller"

    def _bin_dir(self) -> Path:
        return self._runtime_dir() / "bin"

    def _lib_dir(self) -> Path:
        return self._runtime_dir() / "lib"

    def _assert_project_runtime_installed(self) -> None:
        bin_dir = self._bin_dir()
        for name in ("opsx-plan", "opsx-run", "opsx-watch-plan"):
            exe = bin_dir / name
            self.assertTrue(exe.is_file(), f"project executable missing at {exe}")
            self.assertTrue(os.access(str(exe), os.X_OK))

        lib = self._lib_dir()
        for pkg in ("metrics", "pricing", "models", "orchestrator"):
            self.assertTrue(
                lib.joinpath(pkg).is_dir(),
                f"project runtime package '{pkg}' missing in {lib}",
            )

    def _assert_adapter_project_artifacts(self) -> None:
        root = self._project_root()
        # The dsh adapter installs its support files under
        # .opsx-controller/dsh, which the runtime .gitignore must not ignore.
        expected = [
            root / ".opencode" / "commands",
            root / ".opencode" / "agents",
            root / ".claude" / "skills",
            root / ".agents" / "skills",
            root / ".codex" / "agents",
            root / ".opsx-controller" / "dsh" / "agents",
        ]
        for artifact in expected:
            self.assertTrue(artifact.is_dir(), f"project artifact missing at {artifact}")

    def test_universal_project_installs_all_adapter_artifacts_and_runtime(self) -> None:
        proc = _run_universal_project(
            Path(self.home.name), self._project_root(), self.env
        )
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self._assert_adapter_project_artifacts()
        self._assert_project_runtime_installed()
        output = proc.stdout + proc.stderr
        for adapter in self.ADAPTERS:
            self.assertIn(f"[install] {adapter}: OK", output)

    def test_universal_project_runtime_matches_repo_copy(self) -> None:
        """The installed executables and runtime packages match the repo copy
        byte-for-byte, proving they are self-contained copies."""
        proc = _run_universal_project(
            Path(self.home.name), self._project_root(), self.env
        )
        self.assertEqual(proc.returncode, 0, proc.stderr)

        bin_dir = self._bin_dir()
        self.assertEqual(
            hashlib.sha256((bin_dir / "opsx-plan").read_bytes()).digest(),
            hashlib.sha256(_REPO.joinpath("orchestrator", "opsx-plan.py").read_bytes()).digest(),
        )
        self.assertEqual(
            hashlib.sha256((bin_dir / "opsx-watch-plan").read_bytes()).digest(),
            hashlib.sha256(_REPO.joinpath("scripts", "opsx-watch-plan").read_bytes()).digest(),
        )

        installed_pkg = self._lib_dir() / "orchestrator"
        repo_pkg = _REPO / "lib" / "orchestrator"
        repo_files = sorted(p.relative_to(repo_pkg) for p in repo_pkg.rglob("*.py"))
        self.assertTrue(repo_files, "expected lib/orchestrator to contain .py modules")
        for rel in repo_files:
            installed_file = installed_pkg / rel
            self.assertTrue(installed_file.is_file(), f"missing installed module: {rel}")
            self.assertEqual(
                hashlib.sha256((repo_pkg / rel).read_bytes()).digest(),
                hashlib.sha256(installed_file.read_bytes()).digest(),
                f"installed module differs from repo copy: {rel}",
            )

    def test_project_installed_plan_runs_without_repo_checkout(self) -> None:
        """The project-installed opsx-plan must run report/dashboard from the
        installed runtime alone — no PYTHONPATH, cwd outside the checkout, so
        any import of the repo's lib/ would fail."""
        proc = _run_universal_project(
            Path(self.home.name), self._project_root(), self.env
        )
        self.assertEqual(proc.returncode, 0, proc.stderr)

        target = Path(self.home.name) / "target-repo"
        target.mkdir()
        subprocess.run(["git", "init"], cwd=target, check=True, capture_output=True)
        subprocess.run(
            ["git", "-c", "user.email=test@example.invalid",
             "-c", "user.name=Test User", "commit", "-m", "init", "--allow-empty"],
            cwd=target, check=True, capture_output=True,
        )
        plan_dir = target / ".opsx-plan"
        plan_dir.mkdir(parents=True)
        (plan_dir / "project-plan.toml").write_text(
            '[plan]\nname = "project-plan"\nadapter = "opencode"\n\n'
            '[[changes]]\nid = "prj-only"\nphase = 1\n',
            encoding="utf-8",
        )
        (plan_dir / "telemetry").mkdir()
        (plan_dir / "telemetry" / "project-plan.jsonl").write_text("", encoding="utf-8")
        (plan_dir / "project-plan.state.json").write_text(
            '{"plan": "project-plan", "approvals": [], '
            '"changes": {"prj-only": {"status": "pending", "round": 0, "phase": "pending"}}}',
            encoding="utf-8",
        )

        installed = self._bin_dir() / "opsx-plan"
        env = {k: v for k, v in os.environ.items() if k != "PYTHONPATH"}
        env["HOME"] = self.home.name
        run_proc = subprocess.run(
            [str(installed), "--repo", str(target), "report", ".opsx-plan/project-plan.toml", "--json"],
            cwd=self.home.name,
            env=env,
            capture_output=True,
            text=True,
        )
        self.assertEqual(run_proc.returncode, 0, run_proc.stderr)
        self.assertIn("project-plan", run_proc.stdout)

        dash_proc = subprocess.run(
            [str(installed), "--repo", str(target), "dashboard", ".opsx-plan/project-plan.toml"],
            cwd=self.home.name,
            env=env,
            capture_output=True,
            text=True,
        )
        self.assertEqual(dash_proc.returncode, 0, dash_proc.stderr)
        self.assertTrue(
            (target / ".opsx-plan" / "dashboards" / "project-plan.html").is_file()
        )

    def test_project_runtime_gitignore_ignores_lib_bin_but_not_dsh_support(self) -> None:
        """The project runtime must self-ignore its installed lib/bin/samples
        and per-change state, but must not ignore the dsh adapter's tracked
        support files under .opsx-controller/dsh."""
        proc = _run_universal_project(
            Path(self.home.name), self._project_root(), self.env
        )
        self.assertEqual(proc.returncode, 0, proc.stderr)

        gitignore = self._runtime_dir() / ".gitignore"
        self.assertTrue(gitignore.is_file(), f".gitignore missing at {gitignore}")
        content = gitignore.read_text(encoding="utf-8")
        for line in ("lib/", "bin/", "samples/", "*.json"):
            self.assertIn(line, content)
        self.assertNotIn("dsh/", content)

        # A per-change state file and the runtime dirs must be ignored.
        gitignore_dir = self._project_root()
        subprocess.run(["git", "init"], cwd=gitignore_dir, check=True, capture_output=True)
        subprocess.run(["git", "add", "-A"], cwd=gitignore_dir, check=True, capture_output=True)
        for path in (".opsx-controller/lib", ".opsx-controller/bin", ".opsx-controller/samples"):
            ignored = subprocess.run(
                ["git", "check-ignore", "--quiet", path],
                cwd=gitignore_dir,
                capture_output=True,
            )
            self.assertEqual(
                ignored.returncode, 0,
                f"runtime dir must be gitignored: {path}",
            )

    def test_universal_project_only_opencode_installs_just_opencode_runtime(self) -> None:
        proc = _run_universal_project(
            Path(self.home.name), self._project_root(), self.env,
            "--only", "opencode",
        )
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self._assert_project_runtime_installed()
        root = self._project_root()
        self.assertTrue((root / ".opencode" / "commands").is_dir())
        self.assertFalse((root / ".claude" / "skills").exists())
        self.assertFalse((root / ".agents" / "skills").exists())
        self.assertFalse((root / ".codex" / "agents").exists())
        self.assertFalse((root / ".opsx-controller" / "dsh").exists())
        output = proc.stdout + proc.stderr
        self.assertIn("[install] opencode: OK", output)
        self.assertNotIn("[install] claude-code", output)


if __name__ == "__main__":
    unittest.main()
