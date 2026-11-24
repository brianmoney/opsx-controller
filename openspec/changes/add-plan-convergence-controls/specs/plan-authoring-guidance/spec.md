## ADDED Requirements

### Requirement: Changes have a maximum acceptance surface and a mandatory split rule

The plan-authoring reference and every adapter-facing plan-authoring surface
SHALL size each planned change against all of these dimensions before it is
written:

| Dimension | Maximum before split or narrowing |
|---|---|
| Named surfaces | 3 (one primary plus at most two supporting) |
| Pages / endpoints / routes | 3 total |
| Acceptance scenarios | 5 |
| Tasks | 12 |
| Expected production diff | 400 changed lines (additions + deletions), excluding tests, fixtures, and docs |

A **named surface** is a distinct externally observable contract: an API endpoint
group, a CLI command group, a page/UI route, a freshness or persistence
contract, an error taxonomy, or a cache layer. A page that adds an API endpoint
counts as both.

The mandatory split rule is independent of the numeric limits: a change that
spans **API + CLI + page + freshness + error taxonomy**, or three or more named
surfaces, SHALL be split before it is written. There SHALL NOT be a review
override for this rule.

Exceeding a numeric threshold SHALL require the author to split or narrow the
change. There SHALL be no operator-authorization exception inside the run, and
no exception SHALL waive the mandatory cross-surface split.

The split checkpoint SHALL be the default response to an oversized change, not
another review override. When an exact `split_authorizations` entry exists, the
author SHALL use it. When no exact authorization exists, the author SHALL split
under the plan's scope-preserving rules or narrow and carry the remainder
forward; the author SHALL NOT ask the operator to authorize the split and SHALL
NOT leave the oversized change for corrective rounds to discover.

The `/opsx-plan` authoring command SHALL require a recorded surface assessment
for every change, covering the five dimensions, and SHALL report any change that
was split, narrowed, or proposed for re-scope.

#### Scenario: A five-page change is over the limit
- **WHEN** an author plans one change that writes five pages
- **THEN** the authoring surface requires a split or narrowing before the change
  is written

#### Scenario: The cross-surface set mandates a split
- **WHEN** a change would own an API contract, a CLI command, a page, freshness
  semantics, and an error taxonomy
- **THEN** the authoring surface mandates a split before writing and does not
  permit a review override

#### Scenario: Three named surfaces require a split
- **WHEN** a change spans an API endpoint group, a page, and a cache layer
- **THEN** the authoring surface requires a split or narrows the change to at
  most three named surfaces with one primary concern

#### Scenario: A numeric exception does not waive the cross-surface rule
- **WHEN** the operator authorizes a change above the task or diff limit
- **THEN** the exception is recorded, and a change spanning API + CLI + page +
  freshness + error taxonomy is still split

#### Scenario: The plan records the surface assessment
- **WHEN** an author writes a change into a plan
- **THEN** the change section or the authoring response records its named
  surfaces, pages or endpoints, acceptance scenarios, task count, and expected
  diff size

#### Scenario: The authoring command reports the decision
- **WHEN** a model authors a plan through `/opsx-plan`
- **THEN** the command requires the surface assessment and reports every change
  that was split, narrowed, or proposed for re-scope

#### Scenario: A review override is not a sizing decision
- **WHEN** an oversized change reaches review and the reviewer would accept it
- **THEN** the authoring surface still treats the sizing defect as an authoring
  defect rather than a valid override

### Requirement: Change authoring enumerates the acceptance surface before writing

`opsx-change-writer` SHALL author exactly one concern per change. Before writing
the proposal, design, tasks, and spec deltas, it SHALL enumerate the acceptance
surface the change's behavior implies:

- the error taxonomy: every error class, envelope, and terminal state the change
  can produce;
- scope and registry cases: missing, invalid, valid, disabled, aliased,
  duplicate, and unauthorized cases that apply;
- cache invariants: key composition, collision behavior, invalidation,
  single-flight, stale-while-revalidate, and no-write or no-subprocess rules;
- parity checks: each pair of surfaces that must agree, such as API/CLI/page;
- guards: authentication, authorization, tenant, and limit checks.

The enumeration is the change's acceptance surface. The writer SHALL keep that
surface proportional to the change and SHALL NOT add release-grade hardening to a
`poc` change or enumerate speculative cases the plan has not accepted.

When the enumeration exceeds the maximum surface in *Changes have a maximum
acceptance surface and a mandatory split rule*, the writer SHALL propose a split
before writing. The proposal SHALL name the candidate child slugs and the surface
each child would own. The writer SHALL NOT create the child changes and SHALL NOT
author the oversized change in the hope that review will narrow it.

The change's acceptance scenarios and tasks SHALL cover only the enumerated
surface. A behavior that is not in the enumeration SHALL NOT be silently added
to the change later as a review correction; it is either a new slice or an
explicit accepted change to the plan.

#### Scenario: The error taxonomy is enumerated up front
- **WHEN** a change adds a read path that can fail with permission denied,
  unreadable state, malformed state, and missing state
- **THEN** the writer enumerates those cases and their required envelopes before
  writing the spec delta

#### Scenario: Cache and parity cases are enumerated up front
- **WHEN** a change caches a page result keyed by request parameters and exposes
  it through both an API and a CLI
- **THEN** the writer enumerates the cache-key collision cases and the API/CLI
  parity checks before writing

#### Scenario: An oversized enumeration proposes a split
- **WHEN** the enumerated surface spans more than three named surfaces or the
  mandatory cross-surface set
- **THEN** the writer returns a proposed split naming candidate child slugs and
  their owned surfaces before authoring

#### Scenario: The writer does not create child changes
- **WHEN** the writer proposes a split
- **THEN** it does not create the children, archive anything, or edit the plan;
  it returns the proposal for the orchestrator or operator decision

#### Scenario: Acceptance scenarios stay proportional
- **WHEN** a change has one accepted concern and two supporting surfaces
- **THEN** the writer authors no more than five acceptance scenarios and twelve
  tasks unless the operator records an exception

#### Scenario: A review correction does not silently widen scope
- **WHEN** a behavior appears during review that the change did not enumerate or
  accept
- **THEN** the surface treats it as `adjacent_scope` or a proposed new slice, not
  as an implicit addition to the change
