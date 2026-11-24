## RENAMED Requirements

- FROM: `### Requirement: Two corrective rounds trigger an explicit split-or-continue decision`
- TO: `### Requirement: Two corrective rounds trigger an explicit split-or-re-scope decision`

## MODIFIED Requirements

### Requirement: Two corrective rounds trigger an explicit split-or-re-scope decision

After two corrective rounds and before any further corrective dispatch, the
orchestrator SHALL record an explicit split-or-re-scope decision with its basis.
It SHALL prefer a split when a previously corrected blocker class has reopened,
the blocker set is not shrinking across rounds, or the remaining work is
orthogonal to the parent's stated concern.

The decision set SHALL be exactly one of:

1. **Split** — a structural replacement executed by `opsx-split-materializer`,
   which SHALL be the sole executor of every structural split.
2. **Re-scope** — narrowing the parent to its accepted surface and carrying the
   remainder forward as a new slice with an explicit obligation mapping.
3. **Carry-forward** — recording the remainder as a carry-forward item while
   independent authorized work continues.

The decision set SHALL NOT include a third corrective round, a bounded-continue
option, an operator convergence question, or a terminal stop. The orchestrator
SHALL NOT authorize a third corrective round for the same blocker class, SHALL
NOT stop because a corrective-round count was reached, and SHALL NOT ask the
operator for internal convergence authorization.

A split SHALL be selected from an exact `split_authorizations` entry when the
plan provides one. Where a split is indicated and no exact entry exists, an
unpreauthorized split SHALL be permitted only where the plan's own split rules or
plan text permit it; the standard change-writer SHALL then author the child set
to the same exactness invariants as an authorization — a complete obligation map,
a dependency remap, and an aggregate exit — and the materializer SHALL execute
it. Where the plan reserves split approval to the operator, the orchestrator
SHALL NOT split autonomously: it SHALL honor the plan's declared operator
checkpoint, or re-scope and carry forward. It SHALL NOT invent a materializer or
a plan edit.

A proposed re-scope/authority decision SHALL be recorded as a ledger artifact
naming the parent, the proposed child slugs, the scope and obligation mapping,
the dependency remap, the aggregate exit, and the authority or plan permission
that is missing. It SHALL NOT be returned as a terminal stop and SHALL NOT block
independent authorized work.

Children of a split SHALL inherit the parent's corrective-round consumption by
blocker class. A blocker class that consumed a corrective round in the parent
SHALL NOT receive a fresh two-round budget in a child; a blocker class introduced
by a child's own diff accrues under that child's budget. The ledger SHALL record
the inherited classes and their consumed counts per child.

#### Scenario: A reopened blocker class prefers split
- **WHEN** the same blocker class that a prior corrective round claimed to fix
  is reopened after the second round
- **THEN** the recorded decision prefers a split over another corrective round,
  and the split is executed by `opsx-split-materializer` under the plan's split
  permission

#### Scenario: Non-shrinking blockers prefer split
- **WHEN** the blocking finding count and severity are not shrinking across the
  two corrective rounds
- **THEN** the recorded decision prefers a split over another corrective round

#### Scenario: Orthogonal remaining work prefers split
- **WHEN** the remaining work is orthogonal to the parent's stated concern
- **THEN** the recorded decision prefers a split over another corrective round

#### Scenario: Unauthorized split returns a proposed decision
- **WHEN** a split is indicated but the plan contains no exact authorization and
  its own split rules do not permit an unpreauthorized split
- **THEN** the orchestrator records the concrete proposed re-scope/authority
  decision in the ledger, narrows to the accepted surface, carries the remainder
  forward, and continues, without returning a terminal stop and without stopping
  on the raw corrective-round count

#### Scenario: Continue is one bounded correction with a closure test
- **WHEN** after the second failing review the remaining blocker is
  `introduced_by_diff` inside the accepted surface and the retired bounded-continue
  option is considered
- **THEN** no third corrective round is authorized; the named closure test becomes
  the acceptance condition of the chosen successor slice or carry-forward record,
  and the decision is split, re-scope, or carry-forward

#### Scenario: Independent authorized work continues
- **WHEN** a blocked change awaits a re-scope/authority decision and another
  ready plan change is already authorized
- **THEN** the orchestrator may drive that independent change and does not treat
  the blocked change as authorization to widen scope

#### Scenario: A plan-permitted unpreauthorized split is authored then materialized
- **WHEN** a split is indicated, the plan contains no exact
  `split_authorizations` entry, and the plan's own split rules or text permit an
  unpreauthorized split
- **THEN** the standard change-writer authors the child set with a complete
  obligation map, a dependency remap, and an aggregate exit, and
  `opsx-split-materializer` executes the split

#### Scenario: A plan-reserved split approval is honored
- **WHEN** the plan reserves split approval to its declared operator checkpoint
- **THEN** the orchestrator does not split autonomously; it honors that
  checkpoint, or re-scopes and carries the remainder forward, and it does not
  invent a materializer or a plan edit

#### Scenario: A third corrective round is refused
- **WHEN** the second corrective round's review fails and `MAX_DISPATCHES` still
  has headroom
- **THEN** no third corrective round is authorized, no `cap_override` naming one
  is recorded, and the decision is split, re-scope, or carry-forward

#### Scenario: A split child inherits the parent's consumed rounds
- **WHEN** a blocker class consumed a corrective round in the parent and a child
  owns that obligation
- **THEN** the child's ledger record inherits the consumed count for that class
  and the child's budget for it is not refreshed, while a blocker class new to
  that child's own diff accrues under the child's budget
