"""Pure transition fixture for model-level adaptive plan splitting.

The runtime for adaptive splitting is a prompt contract: a unit test cannot
dispatch ``opsx-orchestrator`` or ``opsx-split-materializer`` and cannot prove
that a live model obeys the prose. This module models the *normative* decisions
the contract requires, so positive and negative cases are deterministic and
stdlib-only:

* exact authorization selection, with general autonomy and role names rejected
  as authority;
* parent eligibility (orchestrator-owned, unaccepted, unarchived, ungated);
* child eligibility (exact, unique, collision-free, unarchived, ungated);
* obligation preservation with no orphan or duplicate;
* dependency remap and aggregate exit;
* freeze-before-rehome, named materializer availability, and no child dispatch
  before a clean bound revision;
* ledger selection/resume/idempotence and fail-closed plan or authorization
  drift;
* the after-two-rounds split-or-re-scope decision: exactly split, re-scope, or
  carry-forward, with a recorded rather than returned proposed decision.

The tests that use this fixture prove the rules above. They do **not** prove
live model behaviour; that remains a contract regression until a real
orchestrator run exercises it.
"""

from __future__ import annotations

from dataclasses import dataclass, field, replace

PARENT = "split-parent"
CHILD_A = "child-alpha"
CHILD_B = "child-beta"
PREREQ = "prereq-change"
SUCCESSOR = "successor-change"
UNRELATED = "unrelated-change"

PARENT_OBLIGATIONS = ("req-cache-policy", "req-cache-expiry", "finding-race", "sp-cache")
AGGREGATE_EXIT = "pytest tests/cache plus the parent's original smoke command"

VALID_STATES = ("selected", "frozen", "materialized", "bound")


@dataclass(frozen=True)
class ChangeNode:
    slug: str
    phase: int
    depends_on: tuple[str, ...]
    obligations: tuple[str, ...] = ()
    acceptance: str = "poc"


@dataclass(frozen=True)
class SplitAuthorization:
    parent: str
    children: tuple[str, ...]
    child_dependencies: dict[str, tuple[str, ...]]
    obligation_map: dict[str, tuple[str, ...]]
    aggregate_exit: str
    rehome: dict[str, tuple[str, ...]] = field(default_factory=dict)


@dataclass(frozen=True)
class PlanState:
    acceptance: str
    human_gates: tuple[str, ...]
    changes: dict[str, ChangeNode]
    authorizations: tuple[SplitAuthorization, ...]
    archived: frozenset[str] = frozenset()
    active: dict[str, str] = field(default_factory=dict)
    external_dependencies: frozenset[str] = frozenset()


@dataclass(frozen=True)
class FreezeRecord:
    donor: str
    path_hashes: dict[str, str]
    lineage: tuple[str, ...]
    restorable: bool = True


@dataclass(frozen=True)
class SplitRecord:
    parent: str
    state: str
    plan_hash: str
    authorization_hash: str = "auth-1"


@dataclass(frozen=True)
class ProposedDecision:
    parent: str
    children: tuple[str, ...]
    scope: str
    aggregate_exit: str
    authority_needed: str
    defect: str = ""


@dataclass(frozen=True)
class SplitDecision:
    ok: bool
    reasons: tuple[str, ...]
    state: str | None = None
    resume_from: str | None = None
    idempotent: bool = False
    proposed: ProposedDecision | None = None


def _default_authorization() -> SplitAuthorization:
    return SplitAuthorization(
        parent=PARENT,
        children=(CHILD_A, CHILD_B),
        child_dependencies={CHILD_A: (PREREQ,), CHILD_B: (CHILD_A,)},
        obligation_map={
            CHILD_A: ("req-cache-policy", "sp-cache"),
            CHILD_B: ("req-cache-expiry", "finding-race"),
        },
        aggregate_exit=AGGREGATE_EXIT,
        rehome={CHILD_A: ("src/cache/policy.py",)},
    )


def valid_plan() -> PlanState:
    return PlanState(
        acceptance="poc",
        human_gates=(),
        changes={
            PREREQ: ChangeNode(PREREQ, 1, (), ("req-prereq",)),
            PARENT: ChangeNode(
                PARENT,
                1,
                (PREREQ,),
                PARENT_OBLIGATIONS,
                acceptance="poc",
            ),
            SUCCESSOR: ChangeNode(SUCCESSOR, 2, (PARENT,), ("req-successor",)),
            UNRELATED: ChangeNode(UNRELATED, 2, (), ("req-unrelated",)),
        },
        authorizations=(_default_authorization(),),
    )


def valid_dependencies() -> dict[str, tuple[str, ...]]:
    return {
        PREREQ: (),
        CHILD_A: (PREREQ,),
        CHILD_B: (CHILD_A,),
        SUCCESSOR: (CHILD_A, CHILD_B),
        UNRELATED: (),
    }


def valid_selected_nodes() -> tuple[str, ...]:
    return (PREREQ, CHILD_A, CHILD_B, SUCCESSOR, UNRELATED)


@dataclass(frozen=True)
class SplitSpec:
    plan: PlanState = field(default_factory=valid_plan)
    parent: str = PARENT
    children: tuple[str, ...] = (CHILD_A, CHILD_B)
    authority: str = "plan"
    plan_hash: str = "plan-h1"
    bound_plan_hash: str = "plan-h1"
    authorization_hash: str = "auth-1"
    parent_accepted: bool = False
    parent_archived: bool = False
    parent_spec_synced: bool = False
    archived_touched: tuple[str, ...] = ()
    tier_changed: bool = False
    phase_changed: bool = False
    human_gates_changed: bool = False
    gates_changed: bool = False
    trust_changed: bool = False
    acceptance_policy_changed: bool = False
    selected_plan_nodes: tuple[str, ...] = field(default_factory=valid_selected_nodes)
    result_dependencies: dict[str, tuple[str, ...]] = field(
        default_factory=valid_dependencies
    )
    obligation_map: dict[str, tuple[str, ...]] | None = None
    aggregate_exit: str | None = None
    dirty_candidate: bool = False
    freeze: FreezeRecord | None = None
    rehome: dict[str, tuple[str, ...]] = field(
        default_factory=lambda: {CHILD_A: ("src/cache/policy.py",)}
    )
    reused_checkmarks: tuple[str, ...] = ()
    completed_changes: frozenset[str] = field(default_factory=lambda: frozenset({PREREQ}))
    materializer_available: bool = True
    named_supervision_declared: bool = False
    child_dispatched_before_bound: bool = False
    ledger: tuple[SplitRecord, ...] = ()
    new_selection: bool = True


def _proposed(defect: str, spec: SplitSpec) -> ProposedDecision:
    return ProposedDecision(
        parent=spec.parent,
        children=spec.children,
        scope="split the parent's obligations across the proposed children",
        aggregate_exit=spec.aggregate_exit
        or (spec.plan.authorizations[0].aggregate_exit if spec.plan.authorizations else ""),
        authority_needed=(
            "an exact split_authorizations entry, a cited plan permission, or an "
            "authorized materializer"
        ),
        defect=defect,
    )


def _fail(spec: SplitSpec, *reasons: str) -> SplitDecision:
    return SplitDecision(
        ok=False,
        reasons=tuple(reasons),
        proposed=_proposed(reasons[0], spec),
    )


def validate_split(spec: SplitSpec) -> SplitDecision:
    """Return whether the normative contract permits this replacement.

    The caller supplies the post-revision facts (selected nodes, dependency
    edges, obligation map, freeze, ledger). A failed result always carries a
    concrete proposed decision; it never reports a raw-count stop.
    """

    if spec.authority != "plan":
        return _fail(spec, f"not-authorized: authority source is {spec.authority!r}")

    matching = [a for a in spec.plan.authorizations if a.parent == spec.parent]
    if len(matching) != 1:
        return _fail(spec, "no-exact-authorization: expected exactly one entry")
    auth = matching[0]

    if tuple(spec.children) != tuple(auth.children):
        return _fail(
            spec,
            "unlisted-child-slug: dispatched children differ from the exact "
            "authorized child list",
        )
    if len(auth.children) < 2 or len(set(auth.children)) != len(auth.children):
        return _fail(spec, "invalid-child-set: children must be two or more unique slugs")

    parent = spec.plan.changes.get(spec.parent)
    if parent is None:
        return _fail(spec, "parent-not-in-plan")
    if spec.parent in spec.plan.human_gates:
        return _fail(spec, "human-gated-parent")
    if spec.parent in spec.plan.archived or spec.parent_archived:
        return _fail(spec, "archived-parent")
    if spec.parent_accepted:
        return _fail(spec, "accepted-parent")
    if spec.parent_spec_synced:
        return _fail(spec, "parent-spec-synced")
    if spec.archived_touched:
        return _fail(
            spec,
            "archived-work-touched: " + ", ".join(spec.archived_touched),
        )

    for child in auth.children:
        if child in spec.plan.changes:
            return _fail(spec, f"child-already-planned: {child}")
        if child in spec.plan.archived:
            return _fail(spec, f"child-archived: {child}")
        if child in spec.plan.human_gates:
            return _fail(spec, f"human-gated-child: {child}")
        occupant = spec.plan.active.get(child)
        if occupant is not None and occupant != child:
            return _fail(spec, f"child-slug-collision: {child} declares {occupant}")

    if (
        spec.tier_changed
        or spec.phase_changed
        or spec.human_gates_changed
        or spec.gates_changed
        or spec.trust_changed
        or spec.acceptance_policy_changed
    ):
        return _fail(
            spec,
            "authority-boundary-change: tier, phase, gates, human boundary, "
            "trust, or acceptance policy changed",
        )

    unmet = [dep for dep in parent.depends_on if dep not in spec.completed_changes]
    if unmet:
        return _fail(spec, "unmet-prerequisite-change: " + ", ".join(unmet))

    for child in auth.children:
        child_dep = auth.child_dependencies.get(child, ())
        if spec.parent in child_dep:
            return _fail(spec, f"child-depends-on-parent: {child}")
        unknown = [
            dep
            for dep in child_dep
            if dep not in spec.selected_plan_nodes
            and dep not in spec.plan.external_dependencies
        ]
        if unknown:
            return _fail(spec, f"unknown-child-dependency: {child} -> {unknown}")

    expected_nodes = (
        set(spec.plan.changes) - {spec.parent}
    ) | set(auth.children)
    if set(spec.selected_plan_nodes) != expected_nodes:
        return _fail(
            spec,
            "selected-plan-node-set: the tracked plan must contain only the "
            "chosen nodes and no parent or unchosen node",
        )

    supplied_map = spec.obligation_map if spec.obligation_map is not None else auth.obligation_map
    seen: dict[str, str] = {}
    for child in auth.children:
        for obligation in supplied_map.get(child, ()):
            if obligation in seen:
                return _fail(
                    spec,
                    f"duplicated-obligation: {obligation} in {seen[obligation]} and {child}",
                )
            seen[obligation] = child
    parent_obligations = set(parent.obligations)
    assigned = set(seen)
    missing = sorted(parent_obligations - assigned)
    extra = sorted(assigned - parent_obligations)
    if missing:
        return _fail(spec, "orphaned-obligation: " + ", ".join(missing))
    if extra:
        return _fail(spec, "unparented-obligation: " + ", ".join(extra))
    if supplied_map != auth.obligation_map:
        return _fail(spec, "obligation-map-mismatch: dispatch differs from authorization")

    if spec.aggregate_exit is not None and spec.aggregate_exit != auth.aggregate_exit:
        return _fail(spec, "aggregate-exit-mismatch")
    if not auth.aggregate_exit.strip():
        return _fail(spec, "missing-aggregate-exit")

    for predecessor in parent.depends_on:
        owners = [
            child
            for child in auth.children
            if predecessor in auth.child_dependencies.get(child, ())
        ]
        if not owners:
            return _fail(spec, f"dropped-predecessor: {predecessor} has no entry child")

    expected_deps = {node: tuple(spec.plan.changes[node].depends_on) for node in spec.plan.changes}
    for child in auth.children:
        expected_deps[child] = tuple(auth.child_dependencies.get(child, ()))
    for node, change in spec.plan.changes.items():
        if node in expected_deps and spec.parent in change.depends_on and node != spec.parent:
            expected_deps[node] = tuple(
                dep for dep in change.depends_on if dep != spec.parent
            ) + tuple(auth.children)
    expected_deps.pop(spec.parent, None)
    if spec.result_dependencies != expected_deps:
        return _fail(
            spec,
            "dependency-remap: actual dependency edges do not preserve every "
            "predecessor and point every successor at the full child set",
        )

    if spec.reused_checkmarks:
        return _fail(
            spec,
            "old-checkmarks-used-as-acceptance: " + ", ".join(spec.reused_checkmarks),
        )

    if spec.dirty_candidate:
        if spec.freeze is None:
            return _fail(spec, "dirty-candidate-without-freeze")
        if not spec.freeze.restorable:
            return _fail(spec, "freeze-not-restorable")
        for paths in spec.rehome.values():
            for path in paths:
                if path not in spec.freeze.path_hashes:
                    return _fail(spec, f"freeze-missing-hash: {path}")
        if not spec.freeze.lineage:
            return _fail(spec, "freeze-missing-lineage")

    if not spec.materializer_available and not spec.named_supervision_declared:
        return _fail(spec, "no-authorized-materializer")

    if spec.child_dispatched_before_bound:
        return _fail(spec, "child-dispatched-before-bound")

    if spec.plan_hash != spec.bound_plan_hash:
        return _fail(spec, "plan-drift: current plan hash differs from the selection")

    non_terminal = [
        record
        for record in spec.ledger
        if record.parent == spec.parent and record.state != "bound"
    ]
    if spec.new_selection and non_terminal:
        return _fail(spec, "second-pending-selection")

    bound = [
        record
        for record in spec.ledger
        if record.parent == spec.parent and record.state == "bound"
    ]
    for record in spec.ledger:
        if record.state not in VALID_STATES:
            return _fail(spec, f"invalid-ledger-state: {record.state}")
        if (
            record.parent == spec.parent
            and record.authorization_hash != spec.authorization_hash
        ):
            return _fail(
                spec,
                "authorization-hash-mismatch: ledger authorization differs from "
                "the current selection",
            )
        if record.parent == spec.parent and record.state != "bound" and record.plan_hash != spec.plan_hash:
            return _fail(spec, "plan-drift: ledger state hash differs")
    if bound:
        if bound[-1].plan_hash != spec.plan_hash:
            return _fail(spec, "plan-drift: bound plan hash differs")
        return SplitDecision(
            ok=True,
            reasons=("already-bound",),
            state="bound",
            idempotent=True,
        )

    partial = [
        record
        for record in spec.ledger
        if record.parent == spec.parent and record.state in VALID_STATES[:-1]
    ]
    if partial:
        return SplitDecision(
            ok=True,
            reasons=(f"resume-{partial[-1].state}",),
            state="bound",
            resume_from=partial[-1].state,
        )

    return SplitDecision(ok=True, reasons=("selected-and-bound",), state="bound")


# ---------------------------------------------------------------------------
# Convergence decision
# ---------------------------------------------------------------------------


#: Corrective rounds one change may consume before the split-or-re-scope
#: decision. The initial review is not a corrective round.
CORRECTIVE_ROUND_BUDGET = 2

#: The pre-budget result plus the three post-budget decision outcomes.
CONVERGENCE_DECISION_KINDS = ("continue", "split", "rescope", "carry-forward")

#: Kinds the contract refuses: no bounded continuation, no operator question, no
#: third corrective round, and no terminal proposed-decision kind.
RETIRED_CONVERGENCE_KINDS = (
    "bounded-continue",
    "operator-question",
    "third-round",
    "proposed-redecision",
)


@dataclass(frozen=True)
class ConvergenceFacts:
    """The facts the after-two-rounds decision reads.

    ``corrective_rounds`` is the change's own total consumption.
    ``consumed_rounds_by_blocker_class`` records how much of it each blocker
    class consumed. ``blocking_blocker_classes`` names the blocker classes of the
    current blocking findings, which is what the exhaustion check reads. The
    split-preference triggers are unchanged: a reopened blocker class, a
    non-shrinking blocker set, or orthogonal remaining work prefers a split. The
    split-permission facts mirror the plan's own gate: an exact
    ``split_authorizations`` entry (``authorization_available``), plan rules or
    text that permit an unpreauthorized split
    (``plan_permits_unpreauthorized_split``), or a plan that reserves split
    approval to its operator checkpoint (``plan_reserves_split_approval``).
    ``inherited_rounds_by_blocker_class`` carries the consumption a split child
    inherits from its parent, keyed by blocker class.
    """

    corrective_rounds: int
    reopened_blocker_class: bool = False
    blocker_counts: tuple[int, ...] = (0, 0)
    orthogonal_work: bool = False
    authorization_available: bool = False
    plan_permits_unpreauthorized_split: bool = False
    plan_reserves_split_approval: bool = False
    inherited_rounds_by_blocker_class: tuple[tuple[str, int], ...] = ()
    consumed_rounds_by_blocker_class: tuple[tuple[str, int], ...] = ()
    blocking_blocker_classes: tuple[str, ...] = ()

    def inherited_rounds(self) -> dict[str, int]:
        """Return the parent consumption this change inherits, by class."""

        return dict(self.inherited_rounds_by_blocker_class)

    def own_rounds(self) -> dict[str, int]:
        """Return this change's own corrective rounds, by blocker class."""

        return dict(self.consumed_rounds_by_blocker_class)

    def consumed_rounds(self) -> int:
        """Return the change's own total corrective-round consumption.

        Inherited consumption is not added here: D16 tracks the budget by
        blocker class, so :func:`remaining_allowance` and
        :func:`exhausted_blocker_classes` decide whether a class still has a
        round. A change with an exhausted inherited class is not itself at the
        decision while its current blocking findings belong to classes that
        still have allowance.
        """

        return self.corrective_rounds


@dataclass(frozen=True)
class ConvergenceDecision:
    """One recorded decision.

    ``proposal_reason`` is a ledger record, never a terminal stop: a decision
    that cannot split records why, then narrows or carries forward and lets
    independent authorized work continue.
    """

    kind: str
    reasons: tuple[str, ...]
    proposal_reason: str | None = None
    independent_work_may_continue: bool = True


def remaining_allowance(facts: ConvergenceFacts, blocker_class: str) -> int:
    """Return the corrective rounds left for one blocker class.

    Consumption is tracked per class (D16): the allowance is
    ``CORRECTIVE_ROUND_BUDGET - inherited - own``, floored at zero. A class
    inherited from the parent starts from the parent's recorded consumption; a
    class new to the child inherits nothing and accrues under the child's own
    budget, so it keeps its full allowance until the child spends a round on it.
    A class inherited at two has no allowance.

    When the facts record no per-class own consumption, a class with no inherited
    record falls back to the change's total consumption, so a caller that only
    knows the total cannot accidentally grant a fresh class budget.
    """

    inherited_map = facts.inherited_rounds()
    own_map = facts.own_rounds()
    inherited = inherited_map.get(blocker_class, 0)
    own = own_map.get(blocker_class)
    if own is None:
        # A per-class record is authoritative; an absent entry means nothing was
        # spent on that class. Only a caller that reports no per-class detail at
        # all falls back to the change's total, which cannot grant a fresh budget
        # to a class it may already have spent.
        own = (
            0
            if (own_map or blocker_class in inherited_map)
            else facts.corrective_rounds
        )
    return max(0, CORRECTIVE_ROUND_BUDGET - inherited - own)


def exhausted_blocker_classes(facts: ConvergenceFacts) -> tuple[str, ...]:
    """Return the current blocking classes that have no remaining allowance.

    Only classes that a current blocking finding belongs to are considered: an
    exhausted inherited class that no current finding names is not a stop.
    """

    return tuple(
        blocker_class
        for blocker_class in facts.blocking_blocker_classes
        if remaining_allowance(facts, blocker_class) == 0
    )


def decide_convergence(facts: ConvergenceFacts) -> ConvergenceDecision:
    """Model the explicit after-two-rounds split-or-re-scope decision.

    Before the budget is consumed the result is ``continue`` with the
    ``within-planning-allowance`` reason. After it, the result is exactly one of
    ``split``, ``rescope``, or ``carry-forward``: never a bounded continuation,
    never a third corrective round, and never a terminal proposed-decision kind.

    Exhaustion is evaluated per blocker class (D16). When the facts name the
    current blocking findings' classes, a corrective round is available only
    while every one of those classes still has allowance, so an exhausted
    inherited class reaches the decision only through a finding of that class and
    a class new to the child keeps its own budget. Without named classes, the
    change's own total consumption is the gate.

    An exact ``split_authorizations`` entry splits, and a plan that permits an
    unpreauthorized split splits after the change-writer authors the child set
    for the materializer to execute. A plan that reserves split approval to its
    operator checkpoint is honored instead of split autonomously. An indicated
    split the plan does not permit records its ``proposal_reason`` and carries
    the remainder forward; with no structural reason to split, the decision
    re-scopes to the accepted surface.
    """

    exhausted = exhausted_blocker_classes(facts)
    if facts.blocking_blocker_classes:
        can_correct = not exhausted
    else:
        can_correct = facts.consumed_rounds() < CORRECTIVE_ROUND_BUDGET
    if can_correct:
        return ConvergenceDecision("continue", ("within-planning-allowance",))

    not_shrinking = (
        len(facts.blocker_counts) >= 2
        and facts.blocker_counts[-1] >= facts.blocker_counts[0]
    )
    split_indicated = (
        facts.reopened_blocker_class or not_shrinking or facts.orthogonal_work
    )
    reasons: list[str] = []
    if exhausted:
        reasons.append("blocker-class-allowance-exhausted")
    if facts.reopened_blocker_class:
        reasons.append("blocker-class-reopened")
    if not_shrinking:
        reasons.append("blockers-not-shrinking")
    if facts.orthogonal_work:
        reasons.append("orthogonal-work")

    if not split_indicated:
        # Nothing structural says the unit is wrong: narrow to the accepted
        # surface and carry the remainder forward as the next slice.
        return ConvergenceDecision(
            "rescope",
            tuple(reasons) + ("rescope-accepted-surface",),
        )

    if facts.authorization_available:
        return ConvergenceDecision(
            "split",
            tuple(reasons) + ("exact-authorization-selected",),
        )

    if facts.plan_reserves_split_approval:
        # The plan's declared checkpoint is a tightening the orchestrator honors;
        # it is not a convergence question to route around, so it governs even
        # where controller policy would permit an unpreauthorized split.
        return ConvergenceDecision(
            "rescope",
            tuple(reasons)
            + ("plan-reserves-split-approval", "honor-plan-checkpoint"),
        )

    if facts.plan_permits_unpreauthorized_split:
        return ConvergenceDecision(
            "split",
            tuple(reasons)
            + (
                "plan-permitted-split",
                "change-writer-authors-child-set",
                "materializer-executes-split",
            ),
        )

    return ConvergenceDecision(
        "carry-forward",
        tuple(reasons) + ("proposed-decision-recorded", "no-exact-authorization"),
        proposal_reason=(
            "a split is indicated but the plan provides neither an exact "
            "split_authorizations entry nor permission for an unpreauthorized "
            "split"
        ),
    )


def with_freeze(spec: SplitSpec, paths: dict[str, str]) -> SplitSpec:
    """Attach a complete, restorable freeze for a dirty-candidate case."""

    return replace(
        spec,
        dirty_candidate=True,
        freeze=FreezeRecord(
            donor=f"openspec/changes/{PARENT}",
            path_hashes=dict(paths),
            lineage=("parent", "selected-plan-hash"),
            restorable=True,
        ),
    )
