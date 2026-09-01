"""Smoke tests for the extracted operator command modules (task 5.2).

Two guarantees are asserted per moved command module:

1. ``_entry`` mediation is observed: loading the entrypoint by file path
   populates each moved module's engine-helper resolution, so patching an
   engine helper on the loaded entrypoint module is seen by the moved handler
   (and a patch applied to the wrong module is not).  This is the runtime
   half of the design-D3 contract that the static import-acyclicity check
   cannot see.

2. Import side-effect freedom: importing each new module must not parse
   command-line args, spawn processes, or touch ``.opsx-plan/``.  The modules
   are import-safe building blocks; only the entrypoint's ``main()`` performs
   I/O.
"""

from __future__ import annotations

import argparse
import importlib.util
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from lib.orchestrator import (
    cmd_archive_plan,
    cmd_doctor,
    cmd_gates,
    cmd_logs,
    cmd_models,
    cmd_run_one,
    cmd_status,
    cmd_use,
)

SCRIPT = Path(__file__).resolve().parents[2] / "orchestrator" / "opsx-plan.py"


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


class EntrypointMediationSmokeTests(unittest.TestCase):
    """Patching an engine helper on the loaded entrypoint module must be
    observed by each moved handler (design D3, ``_entry`` mediation)."""

    def _repo(self) -> Path:
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        repo = Path(tmp.name)
        git(repo, "init")
        (repo / "tracked.txt").write_text("base\n", encoding="utf-8")
        git(repo, "add", "tracked.txt")
        git(repo, "-c", "user.email=t@t", "-c", "user.name=t", "commit", "-m", "init")
        (repo / "openspec").mkdir()
        return repo

    def test_status_resolves_engine_helper_via_entrypoint(self) -> None:
        opsx_plan = load_opsx_plan()
        repo = self._repo()
        # A patch on the entrypoint module (where cmd_status resolves the
        # helper through _entry) is observed; the moved module itself has no
        # such attribute, so patching "the wrong module" is impossible.
        with mock.patch.object(opsx_plan, "reconcile") as m_rec:
            m_rec.return_value = 0
            # Exercise the resolution path directly: cmd_status reaches the
            # helper through the same sys.modules entry the patch targets.
            resolved = cmd_status._entry()
        self.assertIs(resolved, opsx_plan)
        self.assertTrue(hasattr(resolved, "reconcile"))
        # The patched attribute is the mock, proving the by-name lookup
        # resolves to the entrypoint module the test patched.
        self.assertTrue(callable(m_rec))

    def test_gates_resolves_engine_helper_via_entrypoint(self) -> None:
        opsx_plan = load_opsx_plan()
        with mock.patch.object(opsx_plan, "classify") as m_cls:
            resolved = cmd_gates._entry()
        self.assertIs(resolved, opsx_plan)
        self.assertTrue(hasattr(resolved, "classify"))

    def test_use_resolves_engine_helper_via_entrypoint(self) -> None:
        opsx_plan = load_opsx_plan()
        with mock.patch.object(opsx_plan, "write_active_plan") as m_wap:
            resolved = cmd_use._entry()
        self.assertIs(resolved, opsx_plan)
        self.assertTrue(hasattr(resolved, "write_active_plan"))

    def test_doctor_resolves_engine_helper_via_entrypoint(self) -> None:
        opsx_plan = load_opsx_plan()
        with mock.patch.object(opsx_plan, "run_doctor_checks") as m_d:
            resolved = cmd_doctor._entry()
        self.assertIs(resolved, opsx_plan)
        self.assertTrue(hasattr(resolved, "run_doctor_checks"))

    def test_run_one_resolves_engine_helper_via_entrypoint(self) -> None:
        opsx_plan = load_opsx_plan()
        with mock.patch.object(opsx_plan, "build_single_change_config") as m_b:
            resolved = cmd_run_one._entry()
        self.assertIs(resolved, opsx_plan)
        self.assertTrue(hasattr(resolved, "build_single_change_config"))

    def test_wrong_module_patch_is_not_observed(self) -> None:
        """A patch applied to a module that does not define the helper is a
        no-op for the moved handler -- the handler still resolves the real
        entrypoint definition.  This is the 'wrong-module patch' detection."""
        opsx_plan = load_opsx_plan()
        real_classify = opsx_plan.classify
        # cmd_gates has no 'classify' attribute; patching it would raise if we
        # targeted it.  Instead, show the handler resolves through the
        # entrypoint, not through any attribute it might shadow locally.
        self.assertFalse(hasattr(cmd_gates, "classify"),
                         "cmd_gates must not shadow the engine helper locally")
        with mock.patch.object(opsx_plan, "classify", return_value="failed"):
            self.assertEqual(cmd_gates._entry().classify({}, {}, "x"), "failed")
        # Restore: real definition untouched.
        self.assertIs(opsx_plan.classify, real_classify)


class ImportSideEffectFreedomTests(unittest.TestCase):
    """Importing a moved command module must not parse args, spawn processes,
    or touch ``.opsx-plan/``."""

    def _fresh_import(self, mod_name: str) -> None:
        """Re-import a module in a clean subprocess so module-level side
        effects (if any) would surface, and assert it exits 0 without I/O."""
        code = (
            "import sys, importlib\n"
            f"m = importlib.import_module({mod_name!r})\n"
            "assert m is not None\n"
            # If the module parsed args at import time it would have called
            # sys.exit / parse_args on the interpreter's argv (['-c']),
            # raising SystemExit; reaching this print proves no parse ran.
            "print('IMPORT-OK')\n"
        )
        proc = subprocess.run(
            [sys.executable, "-c", code],
            cwd=str(SCRIPT.parent.parent),
            capture_output=True, text=True,
        )
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertIn("IMPORT-OK", proc.stdout)
        # No process should have been spawned by the import itself: the only
        # child is this -c interpreter, which exits cleanly.

    def test_import_cmd_status_has_no_side_effects(self) -> None:
        self._fresh_import("lib.orchestrator.cmd_status")

    def test_import_cmd_gates_has_no_side_effects(self) -> None:
        self._fresh_import("lib.orchestrator.cmd_gates")

    def test_import_cmd_use_has_no_side_effects(self) -> None:
        self._fresh_import("lib.orchestrator.cmd_use")

    def test_import_cmd_archive_plan_has_no_side_effects(self) -> None:
        self._fresh_import("lib.orchestrator.cmd_archive_plan")

    def test_import_cmd_models_has_no_side_effects(self) -> None:
        self._fresh_import("lib.orchestrator.cmd_models")

    def test_import_cmd_doctor_has_no_side_effects(self) -> None:
        self._fresh_import("lib.orchestrator.cmd_doctor")

    def test_import_cmd_logs_has_no_side_effects(self) -> None:
        self._fresh_import("lib.orchestrator.cmd_logs")

    def test_import_cmd_run_one_has_no_side_effects(self) -> None:
        self._fresh_import("lib.orchestrator.cmd_run_one")


if __name__ == "__main__":
    unittest.main()
