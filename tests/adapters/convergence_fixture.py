"""Pure decision fixture for the plan convergence controls.

The runtime for convergence is a prompt contract: a unit test cannot dispatch
``opsx-orchestrator`` and cannot prove that a live model obeys the prose. This
module models the *normative* decisions the contract requires, so positive and
negative cases are deterministic and stdlib-only. It performs no I/O, reads no
clock, and uses no randomness:

* finding classification, with the precedence and the ``unknown``
  pre-adjudication state from the ``plan-handoff-gate`` delta;
* the split / re-scope / carry-forward decision after a broad first review and
  after the two-round corrective budget, with no third-round outcome and no
  operator question;
* honest per-stage token and cost reporting, where an unresolved or missing cost
  is never folded into ``$0``;
* the stale-idle-marker protocol, which waits on a live child and reconciles
  once from the ledger before any re-dispatch.

The tests that use this fixture prove the rules above. They do **not** prove
live model behaviour; that remains a contract regression until a real
orchestrator run exercises it.
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping
from dataclasses import dataclass

#: Corrective rounds a change may consume before the convergence decision. The
#: initial review is not a corrective round.
CORRECTIVE_BUDGET = 2

#: The complete decision set. There is no third-corrective-round value and no
#: operator-question value.
CORRECTIVE_DECISIONS = ("correct", "split", "rescope", "carry-forward")

#: The plan's split permission, as the convergence decision reads it.
SPLIT_AUTHORIZATIONS = ("exact", "plan-permitted", "plan-reserved", "none")

#: The reviewer's ``introduced_by_diff`` values, plus the pre-adjudication
#: ``unknown`` state.
INTRODUCED_BY_DIFF_VALUES = ("yes", "no", "unknown")

#: The three settled classes plus the pre-adjudication ``unknown`` state.
FINDING_CLASSES = (
    "introduced_by_diff",
    "adjacent_scope",
    "pre_existing",
    "unknown",
)

#: A named surface is a distinct externally observable contract.
NAMED_SURFACES = (
    "api-contract",
    "cli",
    "page-ui",
    "freshness",
    "error-taxonomy",
    "cache-invariants",
)

#: Breadth triggers: blocking corrections in two named surfaces, three
#: unprovable scenarios, or three uncorrelated loci make the change broad.
BROAD_SURFACE_COUNT = 2
BROAD_SCENARIO_COUNT = 3
BROAD_LOCUS_COUNT = 3

#: Per-stage cost statuses.
COST_STATUSES = ("estimated", "unresolved", "unavailable", "not-reported")

#: The complete stale-status action set.
STALE_STATUS_DECISIONS = ("wait", "reconcile-once", "process-result")


def classify_finding(adjacent_scope: bool, introduced_by_diff: str) -> str:
    """Derive one finding's class from the reviewer's two fields.

    Precedence, per *Findings are classified before another corrective round*:

    1. ``adjacent_scope: true`` -> ``adjacent_scope``;
    2. else ``introduced_by_diff: yes`` -> ``introduced_by_diff``;
    3. else ``introduced_by_diff: unknown`` -> ``unknown``, which is the
       pre-adjudication state, not a round-earning class: the orchestrator must
       resolve it from the diff and the evidence before any corrective round;
    4. else -> ``pre_existing``.
    """

    if introduced_by_diff not in INTRODUCED_BY_DIFF_VALUES:
        raise ValueError(
            "introduced_by_diff must be one of "
            f"{INTRODUCED_BY_DIFF_VALUES}: {introduced_by_diff!r}"
        )
    if adjacent_scope:
        return "adjacent_scope"
    if introduced_by_diff == "yes":
        return "introduced_by_diff"
    if introduced_by_diff == "unknown":
        return "unknown"
    return "pre_existing"


@dataclass(frozen=True)
class Finding:
    """One review finding, already classified.

    ``classification`` is the value :func:`classify_finding` produced. Only a
    ``blocking`` ``introduced_by_diff`` finding can earn a corrective round.

    ``surface`` optionally tags the finding with the named surface that requires
    the correction. The tag is what the named-surface breadth trigger reads: two
    surfaces that merely exist are not broad, two surfaces that carry blocking
    corrections are.
    """

    classification: str
    blocking: bool = True
    surface: str | None = None

    def __post_init__(self) -> None:
        if self.classification not in FINDING_CLASSES:
            raise ValueError(
                f"classification must be one of {FINDING_CLASSES}: "
                f"{self.classification!r}"
            )
        if self.surface is not None and self.surface not in NAMED_SURFACES:
            raise ValueError(
                f"surface must be one of {NAMED_SURFACES} or None: "
                f"{self.surface!r}"
            )


@dataclass(frozen=True)
class FirstReview:
    """Which review a decision follows, and whether that review was broad.

    ``is_first`` marks a decision taken at the first review. ``broad`` is the
    first review's own sizing verdict (D3). ``implementation_evidence`` marks the
    pre-first-review decision point where implementation itself has already shown
    the change exceeds the maximum surface heuristic; D3 evaluates the checkpoint
    at the earliest evidence, so that decision still runs it.

    The checkpoint belongs to those two points only. A broad flag on a re-review
    is not the first-review checkpoint: a change that could not be bounded at the
    first review is handled by the two-round budget instead, so a re-review
    inside the budget still earns its corrective round.
    """

    is_first: bool = True
    broad: bool = False
    implementation_evidence: bool = False


@dataclass(frozen=True)
class SurfaceFacts:
    """The breadth facts the review reports.

    ``blocking_surfaces`` holds the distinct named surfaces from
    :data:`NAMED_SURFACES` in which blocking findings require correction. That
    association is the named-surface trigger: a change that merely has two
    surfaces is not broad. An unknown surface name is ignored, so a typo cannot
    make a change look broad. ``unprovable_scenarios`` counts accepted scenarios
    no single focused check can prove, and ``uncorrelated_loci`` counts distinct
    blocking loci with no shared root cause.

    :meth:`with_findings` derives the blocking surfaces from the tagged
    findings, so a review's own findings drive the trigger without a second
    hand-maintained list.
    """

    blocking_surfaces: tuple[str, ...] = ()
    unprovable_scenarios: int = 0
    uncorrelated_loci: int = 0

    def is_broad(self) -> bool:
        """Return whether these facts alone make the change broad."""

        known = {
            name for name in self.blocking_surfaces if name in NAMED_SURFACES
        }
        return (
            len(known) >= BROAD_SURFACE_COUNT
            or self.unprovable_scenarios >= BROAD_SCENARIO_COUNT
            or self.uncorrelated_loci >= BROAD_LOCUS_COUNT
        )

    def with_findings(self, findings: Iterable[Finding]) -> "SurfaceFacts":
        """Add every surface a blocking ``introduced_by_diff`` finding names.

        A non-blocking finding, an ``adjacent_scope`` or ``pre_existing``
        finding, and an unadjudicated ``unknown`` finding require no correction
        in this change, so they never contribute a surface.
        """

        tagged = tuple(
            finding.surface
            for finding in findings
            if finding.blocking
            and finding.classification == "introduced_by_diff"
            and finding.surface is not None
        )
        if not tagged:
            return self
        return SurfaceFacts(
            blocking_surfaces=self.blocking_surfaces + tagged,
            unprovable_scenarios=self.unprovable_scenarios,
            uncorrelated_loci=self.uncorrelated_loci,
        )


def _surface_facts(
    surfaces: SurfaceFacts | Iterable[str] | int,
) -> SurfaceFacts:
    """Normalize the ``surfaces`` argument to :class:`SurfaceFacts`.

    A sequence is read as named surfaces in which blocking corrections are
    required; an integer is read as that many such surfaces.
    """

    if isinstance(surfaces, SurfaceFacts):
        return surfaces
    if isinstance(surfaces, int):
        return SurfaceFacts(blocking_surfaces=NAMED_SURFACES[:surfaces])
    return SurfaceFacts(blocking_surfaces=tuple(surfaces))


def convergence_decision(
    rounds_used: int,
    findings: Iterable[Finding],
    first_review: FirstReview | None = None,
    surfaces: SurfaceFacts | Iterable[str] | int = (),
    split_authorization: str = "none",
) -> str:
    """Return exactly one of ``correct|split|rescope|carry-forward``.

    ``findings`` are :class:`Finding` records for the review just completed.
    ``first_review`` is the :class:`FirstReview` state (``None`` means the
    decision does not follow the first review). ``surfaces`` carries the named
    surfaces in which blocking findings require correction, and
    ``split_authorization`` is the plan's split permission: ``exact``,
    ``plan-permitted``, ``plan-reserved``, or ``none``. Surfaces named by the
    blocking findings themselves are merged in.

    Rules:

    * Only blocking ``introduced_by_diff`` findings earn a corrective round.
      ``adjacent_scope``, ``pre_existing``, and ``unknown`` never do; with no
      introduced defect the change has nothing to correct, so adjacent scope
      becomes a new slice only where the plan already permits the split and
      everything else is carried forward.
    * A broad first review, or an exhausted budget (``rounds_used >= 2``), makes
      the split-or-re-scope decision before any further correction. A first
      review is broad when its blocking findings require correction in two or
      more distinct named surfaces, when three or more accepted scenarios cannot
      be proved by one focused check, or when its blocking loci are three or more
      and uncorrelated; a change that merely spans several surfaces is not broad.
      ``exact`` and ``plan-permitted`` authorization split, ``plan-reserved``
      re-scopes, and ``none`` carries forward. An exact authorization is selected
      before correction, so a broad first review at ``rounds_used == 0`` splits.
    * The sizing checkpoint belongs to the first review, and to the
      pre-first-review decision point where implementation evidence already shows
      the change is oversized. A re-review never re-runs it: a change that could
      not be bounded at the first review is handled by the two-round budget, so an
      eligible re-review inside the budget still earns its corrective round.
    * Otherwise a blocking introduced defect earns one corrective round.
    """

    if split_authorization not in SPLIT_AUTHORIZATIONS:
        raise ValueError(
            f"split_authorization must be one of {SPLIT_AUTHORIZATIONS}: "
            f"{split_authorization!r}"
        )
    review = first_review if first_review is not None else FirstReview(is_first=False)
    findings = tuple(findings)
    facts = _surface_facts(surfaces).with_findings(findings)

    blocking_introduced = any(
        finding.blocking and finding.classification == "introduced_by_diff"
        for finding in findings
    )
    if not blocking_introduced:
        # No introduced defect: no corrective round is earned. Adjacent scope
        # becomes a new slice only under a plan-permitted split; adjacent scope
        # without permission, a pre-existing remainder, and any unadjudicated
        # `unknown` are recorded and carried forward.
        blocking_adjacent = any(
            finding.blocking and finding.classification == "adjacent_scope"
            for finding in findings
        )
        if blocking_adjacent and split_authorization in ("exact", "plan-permitted"):
            return "split"
        return "carry-forward"

    # D3's sizing checkpoint is evaluated at the first review, and at the
    # pre-first-review point where implementation evidence already shows the
    # change is oversized. A re-review never re-runs it: the two-round budget
    # governs from there, so an eligible re-review inside the budget still earns
    # its corrective round.
    at_sizing_checkpoint = review.is_first or review.implementation_evidence
    checkpoint = at_sizing_checkpoint and (facts.is_broad() or review.broad)
    if checkpoint or rounds_used >= CORRECTIVE_BUDGET:
        if split_authorization in ("exact", "plan-permitted"):
            return "split"
        if split_authorization == "plan-reserved":
            return "rescope"
        return "carry-forward"
    return "correct"


@dataclass(frozen=True)
class StageUsage:
    """One stage's recorded usage and cost status.

    A missing token field is ``None``: absence is ``not-reported``, never zero.
    A genuine zero is an explicit ``0`` in all three token categories.
    """

    tokens_in: int | None = None
    tokens_out: int | None = None
    cache_read_tokens: int | None = None
    cost_status: str = "estimated"
    estimated_cost: float | None = None


@dataclass(frozen=True)
class CostSummary:
    """The run's token totals and honest cost presentation.

    The three token totals sum only the categories each stage actually reported;
    a missing category contributes nothing and is reflected in that stage's
    ``not-reported`` classification, never treated as a known zero of its own.
    ``priced_subtotal`` sums only resolved estimates. ``unpriced_stage_count``
    counts stages with observed usage whose cost is unresolved or unavailable,
    and ``not_reported_stage_count`` counts stages with no observed usage or with
    a missing token category. ``present_as_zero`` is the only permission to show
    a bare ``$0``/``$0.00`` total: it is true only when at least one stage record
    exists, every stage is either estimated at an explicit zero or reports all
    three token categories as zero, and the subtotal is genuinely zero. A run
    with no stage records carries no evidence of zero usage, so it never presents
    as zero and its burn is unknown.
    """

    tokens_in: int
    tokens_out: int
    cache_read_tokens: int
    priced_subtotal: float
    unpriced_stage_count: int
    not_reported_stage_count: int
    present_as_zero: bool


def _as_stage(stage: StageUsage | Mapping[str, object]) -> StageUsage:
    if isinstance(stage, StageUsage):
        return stage
    return StageUsage(**stage)


def cost_summary(stages: Iterable[StageUsage | Mapping[str, object]]) -> CostSummary:
    """Summarize per-stage tokens and cost without inventing a zero.

    A stage that reports no usage at all is ``not-reported``. A stage with
    observed usage and an ``unresolved`` or ``unavailable`` cost is ``unpriced``
    and is never folded into the subtotal. A stage with an incomplete usage
    record — some but not all three token categories — is ``not-reported`` even
    when its cost is priced, because the missing categories are unknown rather
    than zero and an incomplete record is never a genuine-zero basis. An
    ``estimated`` stage with real usage but no cost figure is unpriced, because
    it cannot be priced.

    A bare ``$0`` presentation is allowed only when nothing is unpriced or
    not-reported and the genuine subtotal is zero, meaning every stage either
    carries an explicit zero estimate or reports all three token categories as
    zero.

    An empty stage iterable has no such evidence: no stage record exists to prove
    a genuine zero, so the summary never presents as zero and the burn is
    unknown. Reporting zero there would claim a fact the run did not establish.
    """

    tokens_in = 0
    tokens_out = 0
    cache_read_tokens = 0
    priced_subtotal = 0.0
    unpriced = 0
    not_reported = 0
    stage_count = 0
    for stage in stages:
        usage = _as_stage(stage)
        stage_count += 1
        if usage.cost_status not in COST_STATUSES:
            raise ValueError(
                f"cost_status must be one of {COST_STATUSES}: "
                f"{usage.cost_status!r}"
            )
        reported = (
            usage.tokens_in,
            usage.tokens_out,
            usage.cache_read_tokens,
        )
        if not any(value is not None for value in reported):
            not_reported += 1
            continue
        tokens_in += usage.tokens_in or 0
        tokens_out += usage.tokens_out or 0
        cache_read_tokens += usage.cache_read_tokens or 0
        complete = all(value is not None for value in reported)
        zero_tokens = not any(reported)
        if usage.cost_status in ("unresolved", "unavailable"):
            unpriced += 1
        elif usage.cost_status == "not-reported":
            not_reported += 1
        elif usage.estimated_cost is None:
            if zero_tokens and complete:
                # All three categories are present and zero: a genuine zero.
                pass
            elif zero_tokens:
                # Some categories are missing, so the zeros that remain are not
                # known zero usage; the stage cannot be a genuine-zero basis.
                not_reported += 1
            else:
                # Real usage with no cost figure cannot be priced.
                unpriced += 1
        elif not complete:
            # The cost is known but a token category is missing: the cost is
            # priced and the usage record is still incomplete.
            priced_subtotal += usage.estimated_cost
            not_reported += 1
        else:
            priced_subtotal += usage.estimated_cost
    return CostSummary(
        tokens_in=tokens_in,
        tokens_out=tokens_out,
        cache_read_tokens=cache_read_tokens,
        priced_subtotal=priced_subtotal,
        unpriced_stage_count=unpriced,
        not_reported_stage_count=not_reported,
        present_as_zero=(
            stage_count > 0
            and not unpriced
            and not not_reported
            and priced_subtotal == 0
        ),
    )


def stale_status_decision(
    ledger_in_flight: bool,
    live_child: bool,
    progress_after_marker: bool,
    terminal_result: bool,
) -> str:
    """Return ``wait|reconcile-once|process-result`` for an idle marker.

    * A terminal result is processed; the marker is not an interruption.
    * A ledger that records the stage in flight with a live child means the idle
      marker is stale: the orchestrator records it and waits. Newer progress
      (``progress_after_marker``) corroborates the live child, but the in-flight
      ledger already contradicts the marker, so waiting does not depend on it.
      Nothing is re-dispatched, cancelled, or duplicated.
    * An in-flight stage with no live child and no terminal result reconciles
      once from the ledger before any re-dispatch.
    * A marker that names no in-flight stage interrupts nothing, so the
      orchestrator records the observation and waits.
    """

    if terminal_result:
        return "process-result"
    # A live child is not interrupted when it produced progress after the marker
    # or when the in-flight ledger contradicts the marker on its own.
    if live_child and (progress_after_marker or ledger_in_flight):
        return "wait"
    if ledger_in_flight:
        return "reconcile-once"
    return "wait"
