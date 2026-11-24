"""Behavior tests for the plan convergence controls.

The runtime is prompt-only, so the cases below exercise the pure decision
protocol in ``convergence_fixture.py``: finding classification, the
split-or-re-scope decision after a broad first review or an exhausted
corrective budget, honest cost presentation, and the stale-idle-marker rule.
They prove the normative protocol, not live model behavior.

Two families live here:

* **Behavior tests** over ``convergence_fixture.py`` — the decision protocol.
* **Static contract tests** over the shipped agent and authoring surfaces. A
  prompt surface is the product, so a missing paragraph is a missing feature.
"""

from __future__ import annotations

import re
import unittest
from pathlib import Path

from tests.adapters import convergence_fixture as fx

_REPO = Path(__file__).resolve().parents[2]

_ORCHESTRATOR = _REPO / "adapters" / "opencode" / "agents" / "opsx-orchestrator.md"
_REVIEW = _REPO / "adapters" / "opencode" / "agents" / "opsx-review.md"
_CHANGE_WRITER = _REPO / "adapters" / "opencode" / "agents" / "opsx-change-writer.md"
_AUDITOR = (
    _REPO / "adapters" / "opencode" / "agents" / "opsx-integration-auditor.md"
)
_HYGIENE = _REPO / "adapters" / "opencode" / "agents" / "opsx-repo-hygiene.md"
_IMPLEMENTER_BODY = _REPO / "core" / "phase-agents" / "implementer.md"
_PLAN_AUTHORING = _REPO / "core" / "plan-authoring.md"
_PLAN_COMMAND = _REPO / "adapters" / "opencode" / "commands" / "opsx-plan.md"


def _text(path: Path) -> str:
    return path.read_text(encoding="utf-8")


def _flat(text: str) -> str:
    """Collapse markdown wrapping and drop emphasis and code markers."""

    return " ".join(
        text.replace("**", "").replace("`", "").replace("*", "").split()
    )


def _section(text: str, heading: str, level: int = 2) -> str:
    """Return the flattened body of one markdown section."""

    marker = "#" * level + " " + heading
    if marker not in text:
        raise AssertionError(f"missing markdown section: {marker!r}")
    rest = text.split(marker, 1)[1]
    stop = re.search(r"\n#{1,%d} " % level, rest)
    body = rest[: stop.start()] if stop else rest
    return _flat(body)


def _table_row(text: str, dimension: str) -> str:
    """Return one markdown table row as ``dimension | value``."""

    for line in text.splitlines():
        cells = [cell.strip() for cell in line.strip().strip("|").split("|")]
        if cells and cells[0] == dimension:
            return " | ".join(cells)
    raise AssertionError(f"missing table row for {dimension!r}")


def _finding(classification: str, blocking: bool = True) -> fx.Finding:
    return fx.Finding(classification=classification, blocking=blocking)


class ClassificationTests(unittest.TestCase):
    """Classification precedence and the pre-adjudication ``unknown`` state."""

    def test_adjacent_scope_wins_over_introduced_by_diff(self) -> None:
        for introduced in fx.INTRODUCED_BY_DIFF_VALUES:
            with self.subTest(introduced=introduced):
                self.assertEqual(
                    fx.classify_finding(True, introduced),
                    "adjacent_scope",
                )

    def test_introduced_yes_is_the_only_round_earning_class(self) -> None:
        self.assertEqual(
            fx.classify_finding(False, "yes"),
            "introduced_by_diff",
        )
        self.assertEqual(fx.classify_finding(False, "no"), "pre_existing")

    def test_unknown_is_its_own_pre_adjudication_state(self) -> None:
        self.assertEqual(fx.classify_finding(False, "unknown"), "unknown")
        self.assertIn("unknown", fx.FINDING_CLASSES)
        self.assertNotEqual(fx.classify_finding(False, "unknown"), "pre_existing")

    def test_an_unrecognized_introduced_value_is_refused(self) -> None:
        with self.assertRaises(ValueError):
            fx.classify_finding(False, "maybe")


class ConvergenceDecisionTests(unittest.TestCase):
    """The decision set is correct | split | rescope | carry-forward."""

    def test_adjacent_only_findings_do_not_earn_a_round(self) -> None:
        findings = (
            _finding(fx.classify_finding(True, "no")),
            _finding(fx.classify_finding(True, "yes")),
        )
        for surfaces in ((), ("api-contract", "cli")):
            for authorization in fx.SPLIT_AUTHORIZATIONS:
                with self.subTest(surfaces=surfaces, authorization=authorization):
                    decision = fx.convergence_decision(
                        rounds_used=0,
                        findings=findings,
                        first_review=fx.FirstReview(broad=False),
                        surfaces=surfaces,
                        split_authorization=authorization,
                    )
                    self.assertNotEqual(decision, "correct")
        # Without split permission the adjacent item is a recorded follow-up.
        self.assertEqual(
            fx.convergence_decision(
                rounds_used=0,
                findings=findings,
                first_review=fx.FirstReview(),
                surfaces=(),
                split_authorization="none",
            ),
            "carry-forward",
        )

    def test_pre_existing_finding_is_carry_forward(self) -> None:
        decision = fx.convergence_decision(
            rounds_used=1,
            findings=(_finding("pre_existing"),),
            first_review=fx.FirstReview(),
            surfaces=(),
            split_authorization="none",
        )
        self.assertEqual(decision, "carry-forward")

    def test_introduced_defect_earns_a_round_within_the_budget(self) -> None:
        for rounds_used in (0, 1):
            with self.subTest(rounds_used=rounds_used):
                decision = fx.convergence_decision(
                    rounds_used=rounds_used,
                    findings=(_finding("introduced_by_diff"),),
                    first_review=fx.FirstReview(),
                    surfaces=("api-contract",),
                    split_authorization="exact",
                )
                self.assertEqual(decision, "correct")

    def test_unknown_classification_is_resolved_before_a_round(self) -> None:
        unresolved = _finding(fx.classify_finding(False, "unknown"))
        for authorization in fx.SPLIT_AUTHORIZATIONS:
            with self.subTest(authorization=authorization):
                self.assertNotEqual(
                    fx.convergence_decision(
                        rounds_used=0,
                        findings=(unresolved,),
                        first_review=fx.FirstReview(),
                        surfaces=(),
                        split_authorization=authorization,
                    ),
                    "correct",
                )
        # Once the orchestrator adjudicates it as introduced, it earns a round.
        self.assertEqual(
            fx.convergence_decision(
                rounds_used=0,
                findings=(_finding(fx.classify_finding(False, "yes")),),
                first_review=fx.FirstReview(),
                surfaces=(),
                split_authorization="none",
            ),
            "correct",
        )

    def test_exhausted_budget_has_no_operator_or_third_round_outcome(self) -> None:
        expected = {
            "exact": "split",
            "plan-permitted": "split",
            "plan-reserved": "rescope",
            "none": "carry-forward",
        }
        seen = set()
        for authorization, outcome in expected.items():
            with self.subTest(authorization=authorization):
                decision = fx.convergence_decision(
                    rounds_used=fx.CORRECTIVE_BUDGET,
                    findings=(_finding("introduced_by_diff"),),
                    first_review=fx.FirstReview(broad=False),
                    surfaces=(),
                    split_authorization=authorization,
                )
                self.assertEqual(decision, outcome)
                seen.add(decision)
        self.assertEqual(seen, {"split", "rescope", "carry-forward"})
        self.assertEqual(
            set(fx.CORRECTIVE_DECISIONS),
            {"correct", "split", "rescope", "carry-forward"},
        )
        for retired in (
            "correct",
            "bounded-continue",
            "operator-question",
            "ask-operator",
            "stop",
            "third-round",
        ):
            with self.subTest(retired=retired):
                self.assertNotIn(retired, seen)
        for forbidden in (
            "bounded-continue",
            "operator-question",
            "ask-operator",
            "stop",
            "third-round",
        ):
            with self.subTest(forbidden=forbidden):
                self.assertNotIn(forbidden, fx.CORRECTIVE_DECISIONS)

    def test_exact_authorization_is_selected_before_correction(self) -> None:
        tagged = (
            fx.Finding(classification="introduced_by_diff", surface="api-contract"),
            fx.Finding(classification="introduced_by_diff", surface="error-taxonomy"),
        )
        for review in (
            fx.FirstReview(is_first=True, broad=True),
            # Two distinct blocking surfaces are broad on their own, without the
            # review's own sizing verdict.
            fx.FirstReview(is_first=True, broad=False),
        ):
            with self.subTest(review=review):
                self.assertEqual(
                    fx.convergence_decision(
                        rounds_used=0,
                        findings=tagged,
                        first_review=review,
                        surfaces=(),
                        split_authorization="exact",
                    ),
                    "split",
                )
        # One blocking surface is not broad, so the exact authorization is not
        # selected before a correction that the budget still allows.
        self.assertEqual(
            fx.convergence_decision(
                rounds_used=0,
                findings=(tagged[0],),
                first_review=fx.FirstReview(is_first=True, broad=False),
                surfaces=(),
                split_authorization="exact",
            ),
            "correct",
        )

    def test_a_broad_first_review_does_not_dispatch_a_correction(self) -> None:
        expected = {
            "exact": "split",
            "plan-permitted": "split",
            "plan-reserved": "rescope",
            "none": "carry-forward",
        }
        tagged = (
            fx.Finding(classification="introduced_by_diff", surface="cli"),
            fx.Finding(classification="introduced_by_diff", surface="freshness"),
        )
        for authorization, outcome in expected.items():
            for findings, review in (
                (
                    (_finding("introduced_by_diff"),),
                    fx.FirstReview(is_first=True, broad=True),
                ),
                (
                    tagged,
                    fx.FirstReview(is_first=True, broad=False),
                ),
            ):
                with self.subTest(authorization=authorization, findings=findings):
                    decision = fx.convergence_decision(
                        rounds_used=0,
                        findings=findings,
                        first_review=review,
                        surfaces=(),
                        split_authorization=authorization,
                    )
                    self.assertEqual(decision, outcome)
                    self.assertNotEqual(decision, "correct")

    def test_a_rereview_inside_the_budget_does_not_rerun_the_checkpoint(self) -> None:
        # ACPC-D3-REVIEW-01: the broad-surface checkpoint belongs to the first
        # review. An eligible re-review inside the two-round budget is a
        # corrective round, whatever surfaces its findings name.
        tagged = (
            fx.Finding(classification="introduced_by_diff", surface="api-contract"),
            fx.Finding(classification="introduced_by_diff", surface="cli"),
        )
        self.assertEqual(
            fx.convergence_decision(
                rounds_used=1,
                findings=tagged,
                first_review=fx.FirstReview(is_first=False),
                split_authorization="exact",
            ),
            "correct",
        )
        # The same input at the first review is broad: two distinct blocking
        # surfaces select the split before any correction.
        self.assertEqual(
            fx.convergence_decision(
                rounds_used=1,
                findings=tagged,
                first_review=fx.FirstReview(is_first=True),
                split_authorization="exact",
            ),
            "split",
        )
        # Pre-first-review implementation evidence still evaluates the
        # checkpoint: D3 places it at the earliest evidence.
        self.assertEqual(
            fx.convergence_decision(
                rounds_used=0,
                findings=tagged,
                first_review=fx.FirstReview(
                    is_first=False, implementation_evidence=True
                ),
                split_authorization="exact",
            ),
            "split",
        )
        # All three breadth triggers are first-review-only on a re-review.
        for facts in (
            fx.SurfaceFacts(blocking_surfaces=("api-contract", "cli")),
            fx.SurfaceFacts(unprovable_scenarios=fx.BROAD_SCENARIO_COUNT),
            fx.SurfaceFacts(uncorrelated_loci=fx.BROAD_LOCUS_COUNT),
        ):
            with self.subTest(facts=facts):
                self.assertEqual(
                    fx.convergence_decision(
                        rounds_used=1,
                        findings=(_finding("introduced_by_diff"),),
                        first_review=fx.FirstReview(is_first=False),
                        surfaces=facts,
                        split_authorization="exact",
                    ),
                    "correct",
                )
        # The exhausted-budget decision is unchanged on a re-review.
        expected = {
            "exact": "split",
            "plan-permitted": "split",
            "plan-reserved": "rescope",
            "none": "carry-forward",
        }
        for authorization, outcome in expected.items():
            with self.subTest(authorization=authorization):
                self.assertEqual(
                    fx.convergence_decision(
                        rounds_used=fx.CORRECTIVE_BUDGET,
                        findings=tagged,
                        first_review=fx.FirstReview(is_first=False),
                        split_authorization=authorization,
                    ),
                    outcome,
                )

    def test_two_blocking_surfaces_make_the_change_broad(self) -> None:
        # D3's named-surface trigger is about the blocking findings, not about
        # how many surfaces the change happens to have.
        api = fx.Finding(classification="introduced_by_diff", surface="api-contract")
        second_api = fx.Finding(
            classification="introduced_by_diff", surface="api-contract"
        )
        cli = fx.Finding(classification="introduced_by_diff", surface="cli")
        non_blocking = fx.Finding(
            classification="introduced_by_diff", blocking=False, surface="cli"
        )
        adjacent = fx.Finding(classification="adjacent_scope", surface="cli")
        pre_existing = fx.Finding(classification="pre_existing", surface="cli")
        unknown = fx.Finding(classification="unknown", surface="cli")

        self.assertFalse(fx.SurfaceFacts().with_findings((api,)).is_broad())
        # Two findings in one surface are still one surface of correction.
        self.assertFalse(fx.SurfaceFacts().with_findings((api, second_api)).is_broad())
        # A surface carries the trigger only through a blocking introduced defect.
        self.assertFalse(
            fx.SurfaceFacts().with_findings((api, non_blocking)).is_broad()
        )
        self.assertFalse(
            fx.SurfaceFacts().with_findings((api, adjacent)).is_broad()
        )
        # A pre_existing carry-forward and an unadjudicated unknown finding
        # require no correction in this change, so their surface tag is inert.
        self.assertFalse(
            fx.SurfaceFacts().with_findings((api, pre_existing)).is_broad()
        )
        self.assertFalse(
            fx.SurfaceFacts().with_findings((api, unknown)).is_broad()
        )
        self.assertTrue(fx.SurfaceFacts().with_findings((api, cli)).is_broad())

        # The findings alone carry the trigger into the decision.
        self.assertEqual(
            fx.convergence_decision(
                rounds_used=0,
                findings=(api, cli),
                first_review=fx.FirstReview(is_first=True, broad=False),
                surfaces=(),
                split_authorization="plan-permitted",
            ),
            "split",
        )
        # The other two triggers are unchanged.
        self.assertEqual(
            fx.convergence_decision(
                rounds_used=0,
                findings=(_finding("introduced_by_diff"),),
                first_review=fx.FirstReview(is_first=True, broad=False),
                surfaces=fx.SurfaceFacts(unprovable_scenarios=3),
                split_authorization="plan-permitted",
            ),
            "split",
        )
        self.assertEqual(
            fx.convergence_decision(
                rounds_used=0,
                findings=(_finding("introduced_by_diff"),),
                first_review=fx.FirstReview(is_first=True, broad=False),
                surfaces=fx.SurfaceFacts(uncorrelated_loci=3),
                split_authorization="plan-permitted",
            ),
            "split",
        )


class CostSummaryTests(unittest.TestCase):
    """Unpriced, genuine zero, and missing usage stay distinguishable."""

    def test_unresolved_cost_is_unpriced_not_zero(self) -> None:
        summary = fx.cost_summary(
            (
                fx.StageUsage(
                    tokens_in=1_200_000,
                    tokens_out=5_000,
                    cache_read_tokens=900_000,
                    cost_status="unresolved",
                ),
                fx.StageUsage(
                    tokens_in=1_000,
                    tokens_out=100,
                    cache_read_tokens=0,
                    cost_status="estimated",
                    estimated_cost=0.25,
                ),
            )
        )
        self.assertEqual(summary.tokens_in, 1_201_000)
        self.assertEqual(summary.tokens_out, 5_100)
        self.assertEqual(summary.cache_read_tokens, 900_000)
        self.assertEqual(summary.unpriced_stage_count, 1)
        self.assertEqual(summary.not_reported_stage_count, 0)
        self.assertAlmostEqual(summary.priced_subtotal, 0.25)
        self.assertFalse(summary.present_as_zero)

    def test_unavailable_cost_is_also_unpriced(self) -> None:
        summary = fx.cost_summary(
            (fx.StageUsage(tokens_in=10, cost_status="unavailable"),)
        )
        self.assertEqual(summary.unpriced_stage_count, 1)
        self.assertFalse(summary.present_as_zero)

    def test_genuine_zero_usage_presents_as_zero(self) -> None:
        summary = fx.cost_summary(
            (
                fx.StageUsage(
                    tokens_in=0,
                    tokens_out=0,
                    cache_read_tokens=0,
                    cost_status="estimated",
                    estimated_cost=0.0,
                ),
                fx.StageUsage(
                    tokens_in=0,
                    tokens_out=0,
                    cache_read_tokens=0,
                    cost_status="estimated",
                ),
            )
        )
        self.assertEqual(summary.tokens_in, 0)
        self.assertEqual(summary.unpriced_stage_count, 0)
        self.assertEqual(summary.not_reported_stage_count, 0)
        self.assertEqual(summary.priced_subtotal, 0)
        self.assertTrue(summary.present_as_zero)

    def test_an_empty_run_is_not_a_genuine_zero(self) -> None:
        # ACPC-F4: no stage record is no evidence of zero usage, so a bare
        # `$0.00` is forbidden and the burn is unknown.
        summary = fx.cost_summary([])
        self.assertEqual(summary.tokens_in, 0)
        self.assertEqual(summary.tokens_out, 0)
        self.assertEqual(summary.cache_read_tokens, 0)
        self.assertEqual(summary.priced_subtotal, 0)
        self.assertAlmostEqual(summary.priced_subtotal, 0.0)
        self.assertEqual(summary.unpriced_stage_count, 0)
        self.assertEqual(summary.not_reported_stage_count, 0)
        self.assertFalse(summary.present_as_zero)

    def test_missing_usage_is_not_reported_and_forbids_the_zero(self) -> None:
        summary = fx.cost_summary(
            (
                fx.StageUsage(),
                fx.StageUsage(
                    tokens_in=0,
                    tokens_out=0,
                    cache_read_tokens=0,
                    cost_status="estimated",
                    estimated_cost=0.0,
                ),
            )
        )
        self.assertEqual(summary.not_reported_stage_count, 1)
        self.assertEqual(summary.unpriced_stage_count, 0)
        self.assertFalse(summary.present_as_zero)
        self.assertEqual(summary.tokens_in, 0)

    def test_incomplete_usage_is_not_a_genuine_zero(self) -> None:
        # One observed zero with two missing categories is an incomplete usage
        # record: the missing categories are unknown, not zero, so the stage
        # cannot support a bare `$0`.
        partial = fx.StageUsage(tokens_in=0, cost_status="estimated")
        summary = fx.cost_summary((partial,))
        self.assertEqual(summary.tokens_in, 0)
        self.assertEqual(summary.unpriced_stage_count, 0)
        self.assertEqual(summary.not_reported_stage_count, 1)
        self.assertFalse(summary.present_as_zero)

        # A partial record with real usage is priced when a figure exists, but
        # the usage record stays incomplete and forbids the zero presentation.
        observed = fx.cost_summary(
            (
                fx.StageUsage(
                    tokens_in=500,
                    cost_status="estimated",
                    estimated_cost=0.1,
                ),
            )
        )
        self.assertEqual(observed.tokens_in, 500)
        self.assertAlmostEqual(observed.priced_subtotal, 0.1)
        self.assertEqual(observed.not_reported_stage_count, 1)
        self.assertFalse(observed.present_as_zero)

        # A complete zero stage beside the incomplete stage does not rescue the
        # presentation: one incomplete stage forbids the bare `$0`.
        mixed = fx.cost_summary(
            (
                fx.StageUsage(
                    tokens_in=0,
                    tokens_out=0,
                    cache_read_tokens=0,
                    cost_status="estimated",
                    estimated_cost=0.0,
                ),
                partial,
            )
        )
        self.assertEqual(mixed.priced_subtotal, 0)
        self.assertEqual(mixed.unpriced_stage_count, 0)
        self.assertEqual(mixed.not_reported_stage_count, 1)
        self.assertFalse(mixed.present_as_zero)

    def test_an_estimated_stage_without_a_figure_is_unpriced(self) -> None:
        summary = fx.cost_summary(
            (fx.StageUsage(tokens_in=42, cost_status="estimated"),)
        )
        self.assertEqual(summary.unpriced_stage_count, 1)
        self.assertEqual(summary.priced_subtotal, 0)
        self.assertFalse(summary.present_as_zero)


class StaleStatusTests(unittest.TestCase):
    """An idle marker interrupts nothing while the ledger is in flight."""

    def test_a_stale_marker_with_a_live_child_waits(self) -> None:
        for progress in (True, False):
            with self.subTest(progress_after_marker=progress):
                self.assertEqual(
                    fx.stale_status_decision(
                        ledger_in_flight=True,
                        live_child=True,
                        progress_after_marker=progress,
                        terminal_result=False,
                    ),
                    "wait",
                )

    def test_a_missing_child_reconciles_once(self) -> None:
        self.assertEqual(
            fx.stale_status_decision(
                ledger_in_flight=True,
                live_child=False,
                progress_after_marker=False,
                terminal_result=False,
            ),
            "reconcile-once",
        )

    def test_a_terminal_result_is_processed(self) -> None:
        for live_child in (True, False):
            with self.subTest(live_child=live_child):
                self.assertEqual(
                    fx.stale_status_decision(
                        ledger_in_flight=True,
                        live_child=live_child,
                        progress_after_marker=True,
                        terminal_result=True,
                    ),
                    "process-result",
                )

    def test_every_result_is_in_the_documented_set(self) -> None:
        for in_flight in (True, False):
            for live_child in (True, False):
                for progress in (True, False):
                    for terminal in (True, False):
                        with self.subTest(
                            in_flight=in_flight,
                            live_child=live_child,
                            progress=progress,
                            terminal=terminal,
                        ):
                            self.assertIn(
                                fx.stale_status_decision(
                                    in_flight, live_child, progress, terminal
                                ),
                                fx.STALE_STATUS_DECISIONS,
                            )


class OrchestratorConvergenceContractTests(unittest.TestCase):
    """`opsx-orchestrator` owns the budget, the classes, and the checkpoint."""

    @classmethod
    def setUpClass(cls) -> None:
        cls.text = _text(_ORCHESTRATOR)
        cls.flat = _flat(cls.text)

    def test_the_corrective_budget_is_two_rounds_and_a_third_is_refused(self) -> None:
        self.assertIn("## Split-or-re-scope after two corrective rounds", self.text)
        section = _section(
            self.text, "Split-or-re-scope after two corrective rounds"
        )
        self.assertIn(
            "A corrective round is one corrective implementation dispatch "
            "followed by the review that verifies it.",
            section,
        )
        self.assertIn("The initial review is not a corrective round.", section)
        self.assertIn(
            "The default budget is two corrective rounds per change",
            section,
        )
        self.assertIn(
            "After the second corrective round ends with a failing review, "
            "record one explicit split-or-re-scope decision with its basis, and "
            "do not grant a third corrective round:",
            section,
        )
        self.assertIn(
            "A third-round cap_override is refused even when MAX_DISPATCHES has "
            "headroom, and a dispatch, repair, or audit override never authorizes "
            "a corrective round.",
            section,
        )
        self.assertIn(
            "There is no operator-authorization path for internal convergence, "
            'and "continue with one bounded corrective round" is not an option.',
            section,
        )
        for kind in ("Split", "Re-scope", "Carry-forward"):
            with self.subTest(kind=kind):
                self.assertIn(f"{kind} —", section)

    def test_findings_are_classified_before_another_round(self) -> None:
        self.assertIn("## Review convergence and finding classification", self.text)
        section = _section(
            self.text, "Review convergence and finding classification"
        )
        self.assertIn(
            "Before dispatching another corrective round, classify every finding "
            "as exactly one of:",
            section,
        )
        self.assertIn(
            "introduced_by_diff — the reviewed diff introduced the observed "
            "failure, or newly exposed or worsened an existing path.",
            section,
        )
        self.assertIn(
            "This is the only class that earns a corrective round.",
            section,
        )
        self.assertIn(
            "adjacent_scope — the behavior is real but outside the change's "
            "accepted success parameters",
            section,
        )
        self.assertIn(
            "pre_existing — the defect predates the reviewed diff and is not "
            "newly exposed or worsened by it.",
            section,
        )
        self.assertIn("adjacent_scope: true → adjacent_scope;", section)
        self.assertIn(
            "else introduced_by_diff: yes → introduced_by_diff;",
            section,
        )
        self.assertIn("else pre_existing.", section)
        self.assertIn(
            "unknown is not a fourth class and SHALL NOT earn a corrective round",
            section,
        )
        self.assertIn(
            "Adjudicate a disputed classification yourself from the evidence "
            "packet; never send it back for another reviewer round.",
            section,
        )

    def test_the_first_review_is_a_split_checkpoint(self) -> None:
        self.assertIn("### Broad first review is a split checkpoint", self.text)
        section = _section(self.text, "Broad first review is a split checkpoint", 3)
        self.assertIn(
            "The first review is a sizing checkpoint as well as a verdict.",
            section,
        )
        for trigger in (
            "its blocking findings require correction in two or more distinct "
            "named surfaces from: API contract, CLI, page/UI, freshness, error "
            "taxonomy, cache invariants;",
            "the change claims three or more acceptance scenarios that cannot be "
            "proved by one focused check; or",
            "its blocking finding set spans three or more distinct loci and is "
            "not explained by one shared root cause.",
        ):
            with self.subTest(trigger=trigger):
                self.assertIn(trigger, section)
        self.assertIn(
            "When the first review is broad, make the split-or-re-scope decision "
            "before dispatching any corrective implementation.",
            section,
        )
        self.assertIn(
            "The same checkpoint applies when implementation evidence shows the "
            "change exceeds the maximum surface heuristic before the first review "
            "runs",
            section,
        )


class ReviewerConvergenceContractTests(unittest.TestCase):
    """`opsx-review` is full-scope, classifies, and never widens scope."""

    @classmethod
    def setUpClass(cls) -> None:
        cls.text = _text(_REVIEW)
        cls.flat = _flat(cls.text)

    def test_every_review_is_full_scope(self) -> None:
        self.assertIn("## Full-scope review discipline", self.text)
        section = _section(self.text, "Full-scope review discipline")
        self.assertIn("The first review and every re-review are full-scope.", section)
        self.assertIn(
            "enumerate every current-slice blocking finding you can establish "
            "from that scope, including findings outside the stated diff that the "
            "accepted scope requires;",
            section,
        )
        self.assertIn(
            "report each prior finding in PRIOR_FINDINGS as closed, still open, "
            "or superseded, and say which;",
            section,
        )
        self.assertIn(
            "A current-slice blocking finding that could have been established in "
            "round 1 from the same evidence",
            section,
        )
        self.assertIn(
            "Record it as a reviewer process defect and do not present it as a "
            "reason for another corrective round",
            section,
        )

    def test_findings_carry_the_two_classification_fields(self) -> None:
        section = _section(self.text, "Findings")
        for field in (
            "introduced_by_diff — yes, no, or unknown;",
            "adjacent_scope — true or false;",
        ):
            with self.subTest(field=field):
                self.assertIn(field, section)
        self.assertIn("adjacent_scope: true → adjacent_scope;", section)
        self.assertIn("else introduced_by_diff: yes → introduced_by_diff;", section)
        self.assertIn("else pre_existing.", section)
        self.assertIn(
            "Only introduced_by_diff findings enter the prose FINDINGS section, "
            "the fix_prompt, or the machine-readable findings array.",
            section,
        )
        self.assertIn(
            "introduced_by_diff: unknown is not a fourth class and does not earn "
            "a round",
            section,
        )

    def test_coverage_is_scoped_and_out_of_scope_is_recorded(self) -> None:
        reporting = _section(self.text, "Reporting coverage")
        for result in ("covered", "not-applicable", "not-covered"):
            with self.subTest(result=result):
                self.assertIn(f"{result} —", reporting)
        self.assertIn(
            "current-slice — the case exercises behavior the change's accepted "
            "success parameters require now.",
            reporting,
        )
        self.assertIn(
            "adjacent — the case concerns a next slice or hardening this change "
            "did not accept",
            reporting,
        )
        self.assertIn(
            "A review that only re-runs the writer's existing tests does not "
            "satisfy this requirement.",
            reporting,
        )
        out_of_scope = _section(self.text, "Out-of-scope records")
        self.assertIn(
            "Return every adjacent_scope and pre_existing finding in the "
            "machine-readable out_of_scope[] array",
            out_of_scope,
        )
        self.assertIn(
            "A pass MAY carry out_of_scope[] entries: they are records and do not "
            "change the verdict.",
            out_of_scope,
        )
        self.assertIn("adjacent_scope — true or false.", out_of_scope)


class ChangeWriterSizingContractTests(unittest.TestCase):
    """`opsx-change-writer` bounds the acceptance surface before writing."""

    @classmethod
    def setUpClass(cls) -> None:
        cls.text = _text(_CHANGE_WRITER)
        cls.flat = _flat(cls.text)

    def test_the_maximum_surface_table_is_published(self) -> None:
        self.assertIn("## Pre-writing surface assessment", self.text)
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
                    _table_row(self.text, dimension),
                    f"{dimension} | {maximum}",
                )

    def test_the_cross_surface_split_is_mandatory(self) -> None:
        section = _section(self.text, "Pre-writing surface assessment")
        self.assertIn(
            "A split proposal is required — split_proposal.required: true — when "
            "either condition holds:",
            section,
        )
        self.assertIn(
            "the change spans API + CLI + page + freshness + error taxonomy, or "
            "three or more named surfaces.",
            section,
        )
        self.assertIn(
            "The second condition is the mandatory split rule. It is independent "
            "of the numeric limits: no review override, no operator authorization "
            "inside the run, and no recorded exception waives it.",
            section,
        )
        self.assertIn(
            "When a split is required, do not author the oversized change and do "
            "not leave it for review to narrow.",
            section,
        )

    def test_the_acceptance_surface_is_enumerated_before_writing(self) -> None:
        section = _section(self.text, "Acceptance-surface enumeration")
        for item in (
            "error taxonomy — every error class, envelope, and terminal state the "
            "change's behavior can produce;",
            "scope and registry cases — the missing, invalid, valid, disabled, "
            "aliased, duplicate, and unauthorized cases that apply;",
            "cache invariants — key composition, collision behavior, "
            "invalidation, single-flight, stale-while-revalidate, and no-write or "
            "no-subprocess rules;",
            "parity checks — each pair of surfaces that must agree, such as "
            "API/CLI/page;",
            "guards — authentication, authorization, tenant, and limit checks.",
        ):
            with self.subTest(item=item):
                self.assertIn(item, section)
        self.assertIn(
            "It SHALL appear in design.md, in the spec deltas, and in tasks.md "
            "before implementation starts",
            section,
        )
        self.assertIn(
            "Do not let a review correction silently widen the enumerated "
            "acceptance surface.",
            self.flat,
        )

    def test_the_writer_proposes_a_split_and_never_executes_it(self) -> None:
        section = _section(self.text, "Split proposals are proposals only")
        self.assertIn(
            "opsx-split-materializer is the sole executor of every structural "
            "split; a change writer never performs one.",
            section,
        )
        self.assertIn(
            "do not create the proposed child changes, their directories, or "
            "their artifacts;",
            section,
        )
        for field in (
            '"surface_assessment"',
            '"named_surfaces"',
            '"pages_endpoints"',
            '"scenarios"',
            '"tasks"',
            '"expected_diff_lines"',
            '"split_proposal"',
        ):
            with self.subTest(field=field):
                self.assertIn(field, self.text)


class ImplementerRiskOrderContractTests(unittest.TestCase):
    """`opsx-implementer` plan mode leads with the highest-risk invariant."""

    @classmethod
    def setUpClass(cls) -> None:
        cls.text = _text(_IMPLEMENTER_BODY)
        cls.flat = _flat(cls.text)

    def test_work_is_ordered_by_correctness_risk(self) -> None:
        section = _section(self.text, "Plan-orchestrator mode")
        self.assertIn(
            "Order the work by correctness risk, not by page order or visible "
            "progress:",
            section,
        )
        for step in (
            "1. scope and registry validation, including authorization;",
            "2. error taxonomy and envelope behavior;",
            "3. cache invariants and concurrency;",
            "4. freshness and persistence semantics;",
            "5. page/UI wiring;",
            "6. presentation and polish.",
        ):
            with self.subTest(step=step):
                self.assertIn(step, section)
        self.assertIn(
            "A change with no scope or cache layer starts at the highest-risk "
            "invariant it does have rather than jumping to its page or its polish.",
            section,
        )

    def test_verification_is_the_changes_exact_focused_tests(self) -> None:
        section = _section(self.text, "Plan-orchestrator mode")
        self.assertIn(
            "Run the exact focused tests the change defines for each step; those "
            "tests are the evidence for that step, and you do not substitute a "
            "broad repository suite for them.",
            section,
        )
        self.assertIn(
            "Do not perform a broad refactor, rename unrelated code, or add a new "
            "test layer to prove a behavior already proved at a lower layer.",
            section,
        )
        self.assertIn(
            "Return blocked only for a real contract conflict: the corrective "
            "handoff contradicts the accepted artifacts, a required prerequisite "
            "is missing, or the environment cannot run the required check.",
            section,
        )
        self.assertIn("A stylistic question is not a contract conflict", section)

    def test_the_convergence_budget_is_context_not_permission(self) -> None:
        section = _section(self.text, "Plan-orchestrator mode")
        self.assertIn(
            "CONVERGENCE_CYCLE — the corrective rounds this change has already "
            "consumed (0 before the first corrective round).",
            section,
        )
        self.assertIn(
            "CONVERGENCE_BUDGET — the corrective-round budget in force: the "
            "two-round default, or the plan's tighter value when it sets one.",
            section,
        )
        self.assertIn(
            "A corrective round already at or past the budget does not let you "
            "widen scope, skip a focused test, or substitute blocked for the work",
            section,
        )


class OperatorAttestationContractTests(unittest.TestCase):
    """Attestation is authoritative for its subject on every surface."""

    @classmethod
    def setUpClass(cls) -> None:
        cls.orchestrator = _text(_ORCHESTRATOR)
        cls.auditor = _text(_AUDITOR)
        cls.hygiene = _text(_HYGIENE)

    def test_the_orchestrator_records_the_full_attestation_subject(self) -> None:
        self.assertIn("## Operator attestation", self.orchestrator)
        section = _section(self.orchestrator, "Operator attestation")
        for field in (
            "subject — quiescence, authority, gate, evidence, or scope;",
            "statement — the operator's words, verbatim;",
            "scope — the exact change, commit, or artifact it covers;",
            "source — invocation, message, or ledger;",
            "recorded_at — the run timestamp.",
        ):
            with self.subTest(field=field):
                self.assertIn(field, section)
        self.assertIn(
            "It is authoritative for its named subject: do not re-derive, "
            "re-check, or ask again about a state the operator has confirmed.",
            section,
        )
        self.assertIn(
            "A quiescence attestation ends the quiescence question: do not poll "
            "the child, restart a probe, or require a second confirmation.",
            section,
        )
        self.assertIn(
            "Attestation cannot make a false repository claim true, cannot "
            "substitute for a hard content check the auditor can read from HEAD, "
            "and cannot widen the change's accepted scope",
            section,
        )
        self.assertIn(
            "When an attestation conflicts with a hard repository fact, record "
            "the conflict and stop for a human-only decision; never silently "
            "prefer either side.",
            section,
        )

    def test_the_auditor_and_hygiene_accept_and_reuse_recorded_evidence(self) -> None:
        auditor = _section(self.auditor, "Proportional evidence")
        self.assertIn(
            "Accept a recorded operator attestation for the state or authority "
            "question it names, and do not re-derive, re-check, or ask again about "
            "that question.",
            auditor,
        )
        self.assertIn("Reuse recorded ledger evidence.", auditor)
        self.assertIn(
            "read that path at HEAD and compare it with the recorded hash instead "
            "of re-collecting a duplicate",
            auditor,
        )
        self.assertIn("Record each reuse in evidence_reused[].", auditor)
        self.assertIn(
            "An accepted attestation removes a question; it never changes a check "
            "result.",
            auditor,
        )
        self.assertIn(
            "Attestation SHALL NOT waive source-manifest-at-head, plan-at-head, or "
            "archive-commit-implementation-paths.",
            auditor,
        )
        hygiene = _section(self.hygiene, "Operator authority and recorded evidence")
        self.assertIn(
            "An operator attestation is an explicit statement in the invocation, "
            "a dispatch reply, or a recorded message.",
            hygiene,
        )
        self.assertIn(
            "It is authoritative for its named subject, and you SHALL NOT "
            "re-derive, re-check, or ask again about a state the operator has "
            "confirmed.",
            hygiene,
        )
        self.assertIn("Reuse recorded ledger evidence.", hygiene)
        self.assertIn(
            "reference the recorded hash instead of re-collecting a duplicate "
            "transcript",
            hygiene,
        )
        self.assertIn(
            "An attestation authorizes only the named commit or action, never a "
            "wider pathspec.",
            hygiene,
        )


class CostStatusAndBookkeepingContractTests(unittest.TestCase):
    """Cost, stale markers, and bookkeeping are recorded honestly and bounded."""

    @classmethod
    def setUpClass(cls) -> None:
        cls.text = _text(_ORCHESTRATOR)

    def test_per_stage_cost_and_burn_is_recorded(self) -> None:
        self.assertIn("## Per-stage cost and burn", self.text)
        section = _section(self.text, "Per-stage cost and burn")
        for field in (
            "tokens_in, tokens_out, and cache_read_tokens;",
            "cost_status — estimated, unresolved, unavailable, or not-reported;",
            "estimated_cost, and its pricing catalog version when estimated;",
            "usage_source — telemetry, the child's report, or the operator.",
        ):
            with self.subTest(field=field):
                self.assertIn(field, section)
        self.assertIn(
            "The run ledger and the final report carry per-stage tokens and cost "
            "status, run totals for the three token categories, a priced_subtotal, "
            "and an unpriced_stage_count.",
            section,
        )
        self.assertIn(
            "A stage with observed usage and an unresolved or unavailable cost is "
            "unpriced; never fold it into zero.",
            section,
        )
        self.assertIn(
            "A stage with no reported usage is not-reported, and the report says "
            "the burn is unknown rather than inventing a number.",
            section,
        )
        self.assertIn(
            "A bare $0 or $0.00 total is forbidden while any stage is unresolved, "
            "unavailable, or not-reported",
            section,
        )
        final_report = _section(self.text, "Final report")
        self.assertIn(
            "Do not report a bare $0 or $0.00 total while any stage is unpriced "
            "or not-reported.",
            final_report,
        )
        self.assertIn(
            "the priced_subtotal and the unpriced_stage_count",
            final_report,
        )

    def test_a_stale_idle_marker_does_not_interrupt_a_live_child(self) -> None:
        section = _section(self.text, "Status and communication")
        self.assertIn(
            "An idle session marker is an observation, not an interruption.",
            section,
        )
        self.assertIn(
            "record a stale_idle_marker observation with the marker source and the "
            "evidence that the child is live, and continue waiting.",
            section,
        )
        self.assertIn(
            "Do not re-dispatch the stage, cancel the child, open a duplicate "
            "session, or start the next stage.",
            section,
        )
        self.assertIn(
            "Treat a stage as interrupted only when no live child exists, no "
            "terminal result is available, and the ledger records it in flight, "
            "and the absence is confirmed by the harness or the operator; then "
            "reconcile once from the ledger before re-dispatching",
            section,
        )
        ledger = _section(self.text, "Run ledger")
        self.assertIn(
            "Record every stale_idle_marker observation with the marker source and "
            "the live-child evidence used to ignore it.",
            ledger,
        )

    def test_bookkeeping_is_bounded_to_one_writer_per_stage(self) -> None:
        self.assertIn("### Bounded bookkeeping", self.text)
        section = _section(self.text, "Bounded bookkeeping", 3)
        self.assertIn(
            "at most one ledger-writer dispatch per stage, appending the stage "
            "checkpoint and every record-only event in a single batch;",
            section,
        )
        self.assertIn(
            "a record-only update — a status line, a dispatch count, a copied "
            "coverage array — never justifies a separate coder session;",
            section,
        )
        self.assertIn(
            "workers return their records in their normal output contract and you "
            "batch them into the stage-boundary write;",
            section,
        )
        self.assertIn(
            "a stage with no decision change and no new evidence takes no writer "
            "dispatch at all.",
            section,
        )


class PlanAuthoringSizingContractTests(unittest.TestCase):
    """The reference and the `/opsx-plan` command size before writing."""

    @classmethod
    def setUpClass(cls) -> None:
        cls.reference = _text(_PLAN_AUTHORING)
        cls.flat_reference = _flat(cls.reference)
        cls.command = _text(_PLAN_COMMAND)
        cls.flat_command = _flat(cls.command)

    def test_the_reference_publishes_the_maximum_surface(self) -> None:
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
        self.assertIn(
            "A page that adds an API endpoint counts as both.",
            self.flat_reference,
        )
        self.assertIn(
            "or three or more named surfaces, SHALL be split before it is "
            "written. There is no review override for this rule.",
            self.flat_reference,
        )

    def test_the_reference_records_the_convergence_decision(self) -> None:
        section = _section(
            self.reference, "Split-or-Re-scope After Two Corrective Rounds", 3
        )
        self.assertIn(
            "Two corrective rounds are the default budget for one change.",
            section,
        )
        self.assertIn(
            "There is no third corrective round, no bounded-continue option, no "
            "operator convergence question, and no terminal stop on the round "
            "count.",
            section,
        )
        self.assertIn(
            "A proposed re-scope/authority decision — a split the plan does not "
            "permit — is a recorded ledger artifact reported in the run, not a "
            "decision request that ends it.",
            section,
        )
        self.assertIn("Consumption is inherited by blocker class", section)
        self.assertIn(
            "A child that exhausts its inherited allowance enters the same "
            "split-or-re-scope decision as any other change.",
            section,
        )
        self.assertIn(
            "Controller specs set the floor and a plan may tighten but never "
            "loosen them.",
            section,
        )

    def test_the_command_records_and_reports_the_assessment(self) -> None:
        self.assertIn(
            "Apply the maximum-surface heuristic to every change before writing "
            "it",
            self.flat_command,
        )
        self.assertIn(
            "Record the five-dimension surface assessment for every change in its "
            "scope and success parameters",
            self.flat_command,
        )
        self.assertIn(
            "Split a change before writing it when it spans API + CLI + page + "
            "freshness + error taxonomy, or three or more named surfaces.",
            self.flat_command,
        )
        self.assertIn(
            "Report the recorded five-dimension surface assessment for each "
            "change, and every change that was split, narrowed, or proposed for "
            "re-scope with the surface that prompted it.",
            self.flat_command,
        )


if __name__ == "__main__":
    unittest.main()
