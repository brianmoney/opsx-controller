# adaptive-plan-splitting Specification

## Purpose

Define how a plan-level `opsx-orchestrator` run decides that a planned change is
the wrong unit of work and replaces it with a preauthorized, exact set of child
changes. The capability covers sizing a change before implementation, an
explicit split-or-continue decision after two corrective rounds, the exact
`split_authorizations` model-level plan frontmatter contract, eligibility and
obligation/dependency preservation, and the single authorized writer
(`opsx-split-materializer`) that freezes a restorable donor and binds a clean
plan revision before any child is dispatched.

## Requirements

### Requirement: Changes are sized before implementation

Before dispatching implementation for a planned change, the plan-level
orchestrator SHALL record a sizing assessment covering the change's obligations,
expected touched surfaces, expected review surface, and expected corrective
rounds.

When the assessment cannot plausibly fit one implement-review-archive loop, or
when the plan contains an exact split authorization for that parent, the
orchestrator SHALL make an explicit split-or-continue decision before the
implement dispatch. Sizing SHALL NOT be deferred until corrective rounds have
been consumed.

#### Scenario: An oversized change is sized out before implementation
- **WHEN** the pre-implementation sizing assessment shows that a change combines
  orthogonal obligations that cannot plausibly close in one review loop
- **THEN** the orchestrator records the assessment and makes the split-or-
  continue decision before dispatching implementation

#### Scenario: A preauthorized split is recognized at sizing
- **WHEN** a change has an exact `split_authorizations` entry and the sizing
  assessment confirms the parent is oversized
- **THEN** the split decision can be made before any corrective round is spent

#### Scenario: A well-sized change continues
- **WHEN** the sizing assessment finds one concern, bounded obligations, and a
  plausible single-loop review surface
- **THEN** the orchestrator records the assessment and dispatches implementation
  without selecting a split

### Requirement: Split authorizations are exact, model-level named replacements

Plan authoring SHALL support an optional `split_authorizations` frontmatter list.
Each entry SHALL name:

- exactly one parent change slug present in the plan;
- two or more unique, exact child change slugs;
- each child's exact plan dependencies;
- an obligation map assigning every parent obligation to exactly one child;
- a non-empty aggregate exit that proves the parent's original success
  parameters after all children archive;
- an optional exact donor-path rehome map.

The field SHALL be model-level plan data only. It SHALL add no compiled-manifest
key, SHALL NOT be read by deterministic `opsx-plan compile`, and SHALL NOT
create conditional nodes or authorize compilation of prose into a replacement.
When an entry is selected, the tracked plan SHALL be revised to contain only the
chosen nodes; the parent and every unchosen node SHALL be absent.

#### Scenario: An exact preauthorization is selectable
- **WHEN** a plan authorizes a parent to be replaced by two exact child slugs
  with a complete obligation map, dependency map, and aggregate exit
- **THEN** the orchestrator may select that entry when the parent is eligible,
  without another human approval

#### Scenario: A vague split instruction is not authority
- **WHEN** a plan says only that a change may be split if needed, without exact
  child slugs, dependencies, obligations, and an aggregate exit
- **THEN** no replacement is authorized and the orchestrator fails closed with a
  proposed decision

#### Scenario: The selected plan contains only chosen nodes
- **WHEN** a split is selected and its plan revision is bound
- **THEN** the tracked plan contains the exact child nodes and no parent or
  unchosen conditional node, and the deterministic compiler still receives an
  ordinary unconditional plan

### Requirement: Split eligibility preserves tier, gates, human boundary, and history

A split SHALL be eligible only when all of the following hold:

- the parent is in the plan and is not in the resolved human-gate set;
- the parent is unaccepted and not archived;
- no child is human-gated, already active as a different change, archived, or
  already present as a planned node;
- every child remains in the parent's phase and inherits the same effective
  acceptance tier;
- the plan's `acceptance` value, phase tier, gates, `human_gates`, trust or
  privacy posture, and acceptance policy are unchanged;
- no archived change, archived delta, or synced specification is edited.

The parent SHALL NOT be archived or spec-synced as part of the split. The
orchestrator's general autonomy, a `cap_override`, a reviewer verdict, or a
supervisor role SHALL NOT substitute for an exact authorization.

#### Scenario: A human-gated parent is never split
- **WHEN** the parent is in the resolved human-gate set
- **THEN** the orchestrator does not select a split and reports the human-gated
  stop instead of treating the split as internal autonomy

#### Scenario: An accepted or archived parent is not replaced
- **WHEN** the parent is already archived or its accepted implementation is
  frozen
- **THEN** the split is refused and the archived change remains untouched

#### Scenario: General autonomy is not split authority
- **WHEN** the orchestrator's autonomy would otherwise allow an internal
  decision, but no exact `split_authorizations` entry exists
- **THEN** no split is performed and a proposed re-scope/authority decision is
  returned

#### Scenario: A provisioned named supervisor selects the same exact entry
- **WHEN** a named supervision capability explicitly declares the split action
  and receives the exact authorization for an eligible parent
- **THEN** it may select that entry without another human approval and gains no
  authority beyond the exact entry

#### Scenario: An undeclared supervisor capability fails closed
- **WHEN** a supervisor role exists but declares no split capability, or the
  requested entry differs from the exact authorization
- **THEN** the selection is refused and the proposed decision is returned

#### Scenario: The split does not change a gate or tier
- **WHEN** selecting an entry would add or remove a human gate, change a phase
  gate, or change the effective acceptance tier
- **THEN** the entry is rejected before any materialization

### Requirement: The replacement preserves obligations and remaps dependencies

For a selected entry, every parent obligation SHALL map to exactly one child.
The union of child obligations SHALL equal the parent's obligation set with no
missing, extra, or duplicated obligation. Every parent predecessor SHALL remain
a prerequisite of each entry child that requires it; no child SHALL depend on the
parent; every existing successor of the parent SHALL depend on all children.
Unknown dependency slugs and cross-child dependencies not declared in the
authorization SHALL be rejected.

The aggregate exit SHALL be a plan-level completion condition proving the
parent's original success parameters after all children archive. The parent's
checked tasks SHALL NOT be reused as child acceptance.

#### Scenario: Every parent obligation is owned once
- **WHEN** a parent has three requirements, two success parameters, and one
  carried finding, and the obligation map assigns all six exactly once
- **THEN** the replacement passes the obligation-preservation check

#### Scenario: An orphaned obligation blocks the split
- **WHEN** one parent requirement is absent from every child's obligation map
- **THEN** the replacement is rejected and the missing obligation is reported

#### Scenario: A duplicated obligation blocks the split
- **WHEN** the same parent finding appears in two children's obligation maps
- **THEN** the replacement is rejected and the duplication is reported

#### Scenario: Dependency remap preserves each edge
- **WHEN** the parent had one predecessor and two successors
- **THEN** each entry child keeps the predecessor, no child depends on the
  parent, and each successor depends on all children

#### Scenario: A dropped successor edge is rejected
- **WHEN** a successor's dependency on the parent is removed without depending
  on the full child set
- **THEN** the replacement is rejected rather than silently widening or
  narrowing the graph

#### Scenario: Old checkmarks are not child acceptance
- **WHEN** the parent has checked tasks and the donor source is rehomed into a
  child
- **THEN** the child's tasks are authored fresh and remain incomplete until the
  child's own review evidence accepts them

### Requirement: Materialization freezes the donor and binds a clean revision before child dispatch

Before rehoming a dirty candidate, the materialization handoff SHALL record a
restorable freeze containing the donor identity, source and evidence hashes, and
blob or copy lineage. A dirty candidate without a complete freeze record SHALL
block the split.

The materialization SHALL be performed by one named, explicitly authorized
writer (`opsx-split-materializer`) or by a named supervision capability that
declares the action. The plan-level conductor SHALL remain read-only, and only
one writer SHALL mutate the worktree at a time. The materializer SHALL revalidate
the exact authorization, revise the tracked plan, rehome only exact authorized
paths, make a local commit with explicit pathspecs, and verify the selected
plan's compiled graph. It SHALL NOT push, archive, spec-sync, change product
behavior, or edit archived work.

The orchestrator SHALL dispatch no child work until the selected plan revision
is committed at `HEAD`, contains only the chosen nodes, and the graph and hash
are verified and bound in the ledger.

#### Scenario: A dirty candidate is frozen before rehoming
- **WHEN** the parent has uncommitted implementation paths and the authorization
  names exact rehome paths
- **THEN** the materializer records the donor identity, path hashes, and
  restorable lineage before moving any path

#### Scenario: A dirty candidate without a freeze is blocked
- **WHEN** rehoming would move uncommitted candidate source and no freeze record
  exists
- **THEN** the materializer returns blocked and moves nothing

#### Scenario: No child starts from an unbound revision
- **WHEN** the plan revision is written but not committed or the compiled graph
  is not verified and bound
- **THEN** the orchestrator dispatches no child change

#### Scenario: No generic writer is substituted
- **WHEN** `opsx-split-materializer` is unavailable and no named supervision
  capability declares the split action
- **THEN** the orchestrator does not dispatch `opsx-repo-hygiene`,
  `opsx-implementer`, or another worker as an improvised materializer; it
  records the blocked handoff and returns the proposed decision

### Requirement: Selection, ledger state, resume, and idempotence fail closed

The ledger SHALL record a split selection with the authorization hash, parent,
exact children, obligation map, dependency map, aggregate exit, base and
selected plan hashes, freeze manifest, materialization commit, verification
result, and state.

The only valid state progression SHALL be `selected`, `frozen`, `materialized`,
then `bound`. The orchestrator SHALL refuse a second non-terminal selection for
the same parent. On resume it SHALL read the ledger before dispatching work: a
`bound` record with the same plan hash SHALL NOT be materialized again; a
partially completed record SHALL be revalidated against its captured hashes and
resumed from the earliest incomplete state; any parent, child, or plan-hash
mismatch SHALL be plan drift and SHALL fail closed with a proposed decision.

Active or archived child-slug collisions, child slugs not listed in the exact
authorization, an unmet prerequisite change, and an authorization that no longer
matches the plan SHALL each fail closed rather than overwrite, reorder, or
reuse a slug.

#### Scenario: Resume after binding is a no-op
- **WHEN** a resume reads a `bound` split record whose selected plan hash
  matches `HEAD`
- **THEN** no materialization is repeated and the orchestrator proceeds to the
  already-authorized child sequence

#### Scenario: Resume after a partial materialization reconciles
- **WHEN** a resume finds a `frozen` or `materialized` record with matching
  captured hashes
- **THEN** the orchestrator resumes at the earliest incomplete state after
  verifying the freeze and plan hashes

#### Scenario: Plan drift fails closed
- **WHEN** the current plan hash differs from the selected authorization's
  captured plan hash
- **THEN** the orchestrator stops the split, records the drift, and returns a
  proposed decision instead of selecting again silently

#### Scenario: A colliding child slug fails closed
- **WHEN** an active directory at a planned child slug declares a different
  change, or the slug is already archived
- **THEN** the split is refused without renaming, moving, or overwriting that
  change

#### Scenario: An unlisted child slug is refused
- **WHEN** the proposed child set differs from the exact authorized child list
- **THEN** the orchestrator refuses the replacement

#### Scenario: A second pending selection is refused
- **WHEN** a split record for the same parent is not yet `bound` or terminal
- **THEN** a second selection for that parent is refused

### Requirement: Two corrective rounds trigger an explicit split-or-continue decision

After two corrective rounds and before any third corrective-round override, the
orchestrator SHALL record an explicit split-or-continue decision with its basis.
It SHALL prefer a split when a previously corrected blocker class has reopened,
the blocker set is not shrinking across rounds, or the remaining work is
orthogonal to the parent's stated concern.

A split SHALL be selected only when an exact authorization exists. When a split
is indicated but no exact authorization exists, the orchestrator SHALL return a
concrete proposed re-scope/authority decision naming the parent, proposed child
slugs, scope and obligation mapping, dependency remap, aggregate exit, and the
authority needed; it SHALL NOT stop merely because a round count was reached.

A continue decision SHALL permit exactly one bounded corrective round with a
named closure test and a re-evaluation checkpoint. A failed closure test SHALL
lead to a split or a proposed decision, never to a second override for the same
blocker class. Independent work that is already authorized MAY continue while
the decision is pending.

#### Scenario: A reopened blocker class prefers split
- **WHEN** the same blocker class that a prior corrective round claimed to fix
  is reopened after the second round
- **THEN** the recorded decision prefers a split over another override

#### Scenario: Non-shrinking blockers prefer split
- **WHEN** the blocking finding count and severity are not shrinking across the
  two corrective rounds
- **THEN** the recorded decision prefers a split over another override

#### Scenario: Orthogonal remaining work prefers split
- **WHEN** the remaining work is orthogonal to the parent's stated concern
- **THEN** the recorded decision prefers a split over another override

#### Scenario: Unauthorized split returns a proposed decision
- **WHEN** a split is indicated but the plan contains no exact authorization
- **THEN** the orchestrator returns the concrete proposed re-scope/authority
  decision and does not stop on the raw corrective-round count

#### Scenario: Continue is one bounded correction with a closure test
- **WHEN** the orchestrator decides to continue without a split
- **THEN** it permits exactly one corrective round with a named closure test and
  re-evaluation checkpoint, and records that a failed closure test reopens the
  split-or-redecision choice

#### Scenario: Independent authorized work continues
- **WHEN** a blocked change awaits a re-scope/authority decision and another
  ready plan change is already authorized
- **THEN** the orchestrator may drive that independent change and does not treat
  the blocked change as authorization to widen scope
