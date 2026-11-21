"""Contract tests for the plan-level OpenCode orchestrator surface.

These are static contract checks: the orchestrator must stay delegation-only,
the plan-authoring reference must declare the acceptance tier, and the
plan-authoring command must require it.
"""

from __future__ import annotations

import unittest
from pathlib import Path

_REPO = Path(__file__).resolve().parents[2]

_ORCHESTRATOR = _REPO / "adapters" / "opencode" / "agents" / "opsx-orchestrator.md"
_PLAN_AUTHORING = _REPO / "core" / "plan-authoring.md"
_PLAN_COMMAND = _REPO / "adapters" / "opencode" / "commands" / "opsx-plan.md"


class PlanOrchestratorContractTests(unittest.TestCase):
    def test_orchestrator_is_delegation_only(self) -> None:
        text = _ORCHESTRATOR.read_text(encoding="utf-8")
        self.assertIn("edit: deny", text)
        self.assertIn("bash: deny", text)
        self.assertIn("task: allow", text)
        self.assertIn("question: allow", text)
        self.assertIn('"opsx-supervision": allow', text)
        self.assertNotIn("bash: allow", text)
        self.assertNotIn("edit: allow", text)

    def test_orchestrator_resolves_acceptance_tier_with_precedence(self) -> None:
        text = _ORCHESTRATOR.read_text(encoding="utf-8")
        self.assertIn("explicit invocation value", text)
        self.assertIn("plan frontmatter", text)
        self.assertIn("default `poc`", text)
        # The tier must be recorded with its source, not guessed from the title.
        self.assertIn("its source", text)
        self.assertIn("Do not infer the tier from the plan title", text)

    def test_plan_authoring_declares_acceptance(self) -> None:
        text = _PLAN_AUTHORING.read_text(encoding="utf-8")
        self.assertIn("acceptance: poc", text)
        self.assertIn("`poc`", text)
        self.assertIn("`phase`", text)
        self.assertIn("`release`", text)
        self.assertIn("explicit invocation value", text)

    def test_plan_command_requires_acceptance(self) -> None:
        text = _PLAN_COMMAND.read_text(encoding="utf-8")
        self.assertIn("acceptance: poc | phase | release", text)
        self.assertIn("Confirm the frontmatter declares `acceptance`", text)


if __name__ == "__main__":
    unittest.main()
