"""Loader contract tests for the ``pause_before_human_only`` manifest key.

Covers the normalized resolution rules (absent-with-gate resolves
human-only, explicit values preserved), the named invalid-combination
error, strict boolean validation, and unchanged legacy loading for
manifests that never set the key.
"""

from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from lib.orchestrator import base, planref


def _plan(changes_toml: str) -> str:
    return (
        "[plan]\n"
        'name = "pause-flag-test"\n'
        'adapter = "opencode"\n'
        "\n"
        f"{changes_toml}"
    )


class PauseBeforeHumanOnlyTests(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.repo = Path(self._tmp.name)

    def load(self, changes_toml: str) -> dict:
        path = self.repo / "plan.toml"
        path.write_text(_plan(changes_toml), encoding="utf-8")
        return planref.load_plan(path, repo=self.repo)

    # -- resolution cases --

    def test_absent_with_gate_resolves_human_only(self) -> None:
        cfg = self.load(
            "[[changes]]\n"
            'id = "c1"\n'
            "pause_before = true\n"
            "enabled = true\n"
        )
        change = cfg["changes"]["c1"]
        self.assertTrue(change["pause_before"])
        self.assertTrue(
            change["pause_before_human_only"],
            "absent key on a gated change must resolve human-only",
        )

    def test_absent_without_gate_resolves_false(self) -> None:
        cfg = self.load(
            "[[changes]]\n"
            'id = "c1"\n'
            "enabled = true\n"
        )
        change = cfg["changes"]["c1"]
        self.assertFalse(change["pause_before"])
        self.assertFalse(change["pause_before_human_only"])

    def test_explicit_true_with_gate_resolves_human_only(self) -> None:
        cfg = self.load(
            "[[changes]]\n"
            'id = "c1"\n'
            "pause_before = true\n"
            "pause_before_human_only = true\n"
        )
        self.assertTrue(cfg["changes"]["c1"]["pause_before_human_only"])

    def test_explicit_false_with_gate_resolves_delegated(self) -> None:
        cfg = self.load(
            "[[changes]]\n"
            'id = "c1"\n'
            "pause_before = true\n"
            "pause_before_human_only = false\n"
        )
        change = cfg["changes"]["c1"]
        self.assertTrue(change["pause_before"])
        self.assertFalse(change["pause_before_human_only"])

    def test_explicit_false_without_gate_is_inert(self) -> None:
        cfg = self.load(
            "[[changes]]\n"
            'id = "c1"\n'
            "pause_before_human_only = false\n"
        )
        self.assertFalse(cfg["changes"]["c1"]["pause_before_human_only"])

    # -- invalid combination --

    def test_true_without_gate_raises_named_error(self) -> None:
        with self.assertRaises(base.PlanError) as ctx:
            self.load(
                "[[changes]]\n"
                'id = "c1"\n'
                "pause_before_human_only = true\n"
            )
        message = str(ctx.exception)
        self.assertIn("pause_before_human_only", message)
        self.assertIn("c1", message)

    # -- strict boolean validation --

    def test_non_boolean_string_value_raises_named_error(self) -> None:
        with self.assertRaises(base.PlanError) as ctx:
            self.load(
                "[[changes]]\n"
                'id = "c1"\n'
                "pause_before = true\n"
                'pause_before_human_only = "yes"\n'
            )
        message = str(ctx.exception)
        self.assertIn("pause_before_human_only", message)
        self.assertIn("c1", message)

    def test_non_boolean_integer_value_raises_named_error(self) -> None:
        with self.assertRaises(base.PlanError) as ctx:
            self.load(
                "[[changes]]\n"
                'id = "c1"\n'
                "pause_before = true\n"
                "pause_before_human_only = 1\n"
            )
        message = str(ctx.exception)
        self.assertIn("pause_before_human_only", message)
        self.assertIn("c1", message)

    # -- legacy loading is unchanged --

    def test_legacy_manifest_without_key_loads_unchanged(self) -> None:
        cfg = self.load(
            "[[changes]]\n"
            'id = "c1"\n'
            "phase = 2\n"
            "depends_on = []\n"
            "pause_before = true\n"
            "enabled = true\n"
        )
        change = cfg["changes"]["c1"]
        self.assertEqual(change["id"], "c1")
        self.assertEqual(change["phase"], 2)
        self.assertEqual(change["depends_on"], [])
        self.assertTrue(change["pause_before"])
        self.assertTrue(change["enabled"])
        # Every pre-existing field keeps its historic value; only the new
        # key appears, resolved to the context-dependent default.
        self.assertTrue(change["pause_before_human_only"])


if __name__ == "__main__":
    unittest.main()
