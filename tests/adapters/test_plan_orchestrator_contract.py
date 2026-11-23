"""Contract tests for the plan-level OpenCode orchestrator surface.

These are static contract checks: the orchestrator must stay delegation-only,
the plan-authoring reference must declare the acceptance tier and the optional
`human_gates` stop list, and the plan-authoring command must require both.
"""

from __future__ import annotations

import unittest
from pathlib import Path

_REPO = Path(__file__).resolve().parents[2]

_ORCHESTRATOR = _REPO / "adapters" / "opencode" / "agents" / "opsx-orchestrator.md"
_PLAN_AUTHORING = _REPO / "core" / "plan-authoring.md"
_PLAN_COMMAND = _REPO / "adapters" / "opencode" / "commands" / "opsx-plan.md"


def _flat(text: str) -> str:
    """Collapse markdown line wrapping so multi-line phrases stay assertable."""
    return " ".join(text.split())


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

    def test_budget_is_opt_in_and_has_no_invented_default(self) -> None:
        text = _ORCHESTRATOR.read_text(encoding="utf-8")
        self.assertNotIn("The default plan budget", text)
        self.assertNotIn("default per-change dispatch budget", text)
        self.assertIn("No default", text)
        self.assertIn("MAX_DISPATCHES", text)
        self.assertIn("Check the budget before every dispatch", text)
        self.assertIn("when a child completes", text)
        self.assertIn("never reset or silently extend a budget", text)

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

    def test_plan_authoring_declares_human_gates(self) -> None:
        text = _PLAN_AUTHORING.read_text(encoding="utf-8")
        flat = _flat(text)
        # The frontmatter example carries the field and marks it optional.
        self.assertIn("human_gates:", text)
        self.assertIn("optional", text)
        self.assertIn(
            "`human_gates` is an optional list of exact change slugs "
            "declared in the same plan",
            flat,
        )
        # A listed change is operator-gated and never started.
        self.assertIn("operator-gated for a model-level plan run", flat)
        self.assertIn("the `opsx-orchestrator` stops before it", flat)
        self.assertIn(
            "does not author, implement, review, or archive it", flat
        )
        self.assertIn("reports the resume instruction", flat)
        self.assertIn(
            "A later change whose dependency is a gated change is not "
            "started either",
            flat,
        )

    def test_plan_authoring_distinguishes_human_gates_from_pause_before(self) -> None:
        text = _PLAN_AUTHORING.read_text(encoding="utf-8")
        flat = _flat(text)
        self.assertIn(
            "distinct from `pause_before`, and neither substitutes for the "
            "other",
            flat,
        )
        self.assertIn("a run-level stop before the change", flat)
        self.assertIn("compiled TOML manifest", flat)
        self.assertIn("an approval step in the compiled DAG", flat)

    def test_plan_authoring_human_gates_absence_and_unknown_slug(self) -> None:
        text = _PLAN_AUTHORING.read_text(encoding="utf-8")
        flat = _flat(text)
        self.assertIn(
            "An absent or empty `human_gates` field means no human gates",
            flat,
        )
        self.assertIn(
            "every planned change is treated as orchestrator-owned", flat
        )
        self.assertIn("authoring defect", flat)
        self.assertIn("fails closed at preflight", flat)
        # The field stays out of the manifest and out of the compiled path.
        self.assertIn("adds no manifest key", flat)

    def test_plan_command_requires_human_gates(self) -> None:
        text = _PLAN_COMMAND.read_text(encoding="utf-8")
        flat = _flat(text)
        self.assertIn(
            "Declare `human_gates` in the frontmatter when the plan contains "
            "an operator-gated change",
            flat,
        )
        self.assertIn(
            "every listed slug must also appear as a `### Change:` heading",
            flat,
        )
        self.assertIn("not a compiled-manifest `pause_before` gate", flat)
        # Authoring defects are fixed before the plan is reported complete.
        self.assertIn("authoring defect", flat)
        self.assertIn("preflight", flat)
        # The final response must report the declared gates.
        self.assertIn("the declared `human_gates`", flat)

    def test_plan_command_documents_human_gates_override(self) -> None:
        text = _PLAN_COMMAND.read_text(encoding="utf-8")
        flat = _flat(text)
        self.assertIn(
            "`HUMAN_GATES` value passed to a model-level run replaces the "
            "frontmatter list for that run",
            flat,
        )
        self.assertIn("runtime input, not an authoring edit", flat)


if __name__ == "__main__":
    unittest.main()
