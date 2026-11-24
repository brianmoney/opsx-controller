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


def _table_row(text: str, dimension: str) -> str:
    """Return one markdown table row as ``dimension | value``."""
    for line in text.splitlines():
        cells = [cell.strip() for cell in line.strip().strip("|").split("|")]
        if cells and cells[0] == dimension:
            return " | ".join(cells)
    raise AssertionError(f"missing table row for {dimension!r}")


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


class MaximumSurfaceHeuristicTests(unittest.TestCase):
    """Plan authoring sizes a change against all five surfaces before writing."""

    @classmethod
    def setUpClass(cls) -> None:
        cls.reference = _PLAN_AUTHORING.read_text(encoding="utf-8")
        cls.flat_reference = _flat(cls.reference)
        cls.command = _PLAN_COMMAND.read_text(encoding="utf-8")
        cls.flat_command = _flat(cls.command)

    def test_reference_publishes_every_dimension_and_its_maximum(self) -> None:
        self.assertIn("### Maximum Change Surface", self.reference)
        expected = {
            "Named surfaces": "3 (one primary plus at most two supporting)",
            "Pages / endpoints / routes": "3 total",
            "Acceptance scenarios": "5",
            "Tasks": "12",
            "Expected production diff": (
                "400 changed lines (additions + deletions), excluding tests, "
                "fixtures, and docs"
            ),
        }
        for dimension, maximum in expected.items():
            with self.subTest(dimension=dimension):
                self.assertEqual(
                    _table_row(self.reference, dimension),
                    f"{dimension} | {maximum}",
                )

    def test_reference_defines_a_named_surface(self) -> None:
        self.assertIn(
            "is a distinct externally observable contract:",
            self.flat_reference,
        )
        self.assertIn(
            "an API endpoint group, a CLI command group, a page/UI route, a "
            "freshness or persistence contract, an error taxonomy, or a cache layer.",
            self.flat_reference,
        )
        self.assertIn(
            "A page that adds an API endpoint counts as both.",
            self.flat_reference,
        )

    def test_reference_mandates_the_cross_surface_split(self) -> None:
        self.assertIn(
            "The mandatory split rule is independent of the numeric limits: a "
            "change that spans",
            self.flat_reference,
        )
        self.assertIn(
            "API + CLI + page + freshness + error taxonomy",
            self.flat_reference,
        )
        self.assertIn(
            "or three or more named surfaces, SHALL be split before it is "
            "written. There is no review override for this rule.",
            self.flat_reference,
        )
        self.assertIn(
            "Exceeding a numeric threshold requires the author to split or "
            "narrow the change.",
            self.flat_reference,
        )

    def test_command_records_the_five_dimension_assessment(self) -> None:
        self.assertIn(
            "Apply the maximum-surface heuristic to every change before writing "
            "it: at most 3 named surfaces (one primary plus at most two "
            "supporting), 3 pages/endpoints/routes, 5 acceptance scenarios, 12 "
            "tasks, and 400 expected production changed lines (additions + "
            "deletions), excluding tests, fixtures, and docs.",
            self.flat_command,
        )
        self.assertIn(
            "A named surface is a distinct externally observable contract",
            self.flat_command,
        )
        self.assertIn(
            "Record the five-dimension surface assessment for every change in "
            "its scope and success parameters",
            self.flat_command,
        )

    def test_command_mandates_the_cross_surface_split(self) -> None:
        self.assertIn(
            "Split a change before writing it when it spans API + CLI + page + "
            "freshness + error taxonomy, or three or more named surfaces.",
            self.flat_command,
        )
        self.assertIn(
            "That rule is independent of the numeric limits and has no review "
            "override; a recorded numeric exception does not waive it either.",
            self.flat_command,
        )

    def test_command_self_verifies_and_reports_the_assessment(self) -> None:
        self.assertIn(
            "Verify every change against the maximum-surface heuristic: named "
            "surfaces, pages/endpoints/routes, acceptance scenarios, tasks, and "
            "expected production diff.",
            self.flat_command,
        )
        self.assertIn(
            "Report the recorded five-dimension surface assessment for each "
            "change, and every change that was split, narrowed, or proposed for "
            "re-scope with the surface that prompted it.",
            self.flat_command,
        )
        self.assertIn(
            "never omit the assessment or the decision",
            self.flat_command,
        )


if __name__ == "__main__":
    unittest.main()
