# plan-authoring-guidance Specification

## Purpose
Provide a single client-neutral, discoverable contract for writing compilable
and appropriately scoped OpenSpec implementation plans across all adapters.

## Requirements

### Requirement: The reference defines the plan-authoring contract

The project SHALL provide a client-neutral reference that documents the
required plan frontmatter, `## Phase N:` and `### Change:` structure, field
ordering, closing sections, eleven machine-readable compile rules, and all
four supported dependency forms.

#### Scenario: A model reads the reference before authoring

- **WHEN** a model follows the project guidance to author a plan
- **THEN** it can find the document structure, dependency syntax, and compile
  conventions in one reference without relying on an invocation-scoped
  command body

#### Scenario: A plan uses dependency syntax

- **WHEN** an authored plan expresses a change dependency
- **THEN** the reference identifies the supported dependency forms and the
  forms that produce compiler dependency edges

### Requirement: The reference distinguishes command namespaces and current OpenSpec facts

The reference and every adapter-facing plan-authoring surface SHALL distinguish
upstream OpenSpec per-change commands from opsx-controller plan-level commands.
The surfaces SHALL direct authors to the installed `plan-authoring.md`
reference, state that authoring command spellings must match the client
registration in the repository, and document current OpenSpec facts including
`skip_specs: true` for docs-only or refactor changes and nested capability
folders under `openspec/specs/`.

#### Scenario: A model selects a command from project guidance

- **WHEN** a model reads an installed project's guidance before proposing,
  applying, archiving, compiling, or running work
- **THEN** it is pointed to the shared plan-authoring reference and can
  distinguish upstream per-change commands from opsx-controller plan-level
  commands

#### Scenario: Adapter guidance uses registered command spelling

- **WHEN** a model follows an adapter's plan-authoring surface
- **THEN** the surface tells it to verify the actual client registration rather
  than assuming one client's slash-command namespace

#### Scenario: A docs-only change has no behavior delta

- **WHEN** a model authors a docs-only or refactor OpenSpec change
- **THEN** the reference explains when to declare `skip_specs: true` instead
  of inventing a behavioral requirement

### Requirement: The reference guides loop-aware plan scoping and validation

The reference SHALL provide heuristics for one-concern, single-loop changes;
executable success parameters; create-stage scope discipline; real-only
dependency edges; judgment-based manual gates; the runnable-horizon rule;
security and data-integrity sequencing; and self-verification including an
`opsx-plan compile` self-check and a pointer to the plan manifest skill for
the TOML half.

#### Scenario: A model scopes a multi-stage request

- **WHEN** a request contains multiple implementation concerns or stages
- **THEN** the model can use the reference to split work into appropriately
  sized changes with executable success parameters and real dependencies

#### Scenario: A model verifies a plan

- **WHEN** the model finishes authoring a plan
- **THEN** it performs the documented structural and compile self-checks and
  reports any unavailable prerequisite honestly

### Requirement: Every adapter installs the reference in discoverable support paths

The OpenCode, Claude Code, and Codex CLI adapters SHALL deploy the same
reference into their global and project-level controller support directories.
Their project guidance snippets and plan-authoring surfaces SHALL point to the
project-level reference first and the global reference second. The installer
output and verification mode SHALL identify the deployed reference path and its
installation status.

#### Scenario: Global adapter installation deploys the reference

- **WHEN** an operator runs any adapter installer globally
- **THEN** the reference is present in that adapter's user-level
  `opsx-controller` support directory

#### Scenario: Project adapter installation deploys the reference

- **WHEN** an operator installs any adapter into a project
- **THEN** the reference is present in that project's adapter support
  directory and the installer verifies it

#### Scenario: A missing reference is reported honestly

- **WHEN** neither the project-level nor global reference exists
- **THEN** the plan-authoring surface reports that the shared guidance is
  unavailable instead of reproducing a second convention or claiming it was
  read

### Requirement: The reference documents the manual task marker

The plan-authoring reference SHALL document the `(manual)` task marker
convention and SHALL direct authors of changes intended for unattended
`opsx-plan` runs to either keep side-effecting live-runtime verification
out of `tasks.md` (expressing it as operator follow-up in the proposal or
design, or covering it with automated tests) or mark the corresponding task
lines with `(manual)`. The reference SHALL state that an unchecked
automatable task fails the run before archive, so an unmarked manual task
in an unattended run is an authoring defect.

#### Scenario: Author plans live-runtime verification for an unattended change

- **WHEN** an author writes a tasks file for a change that will run
  unattended and one task requires planting fixtures in a live runtime
- **THEN** the reference directs the author to mark that task `(manual)` or
  move it out of the tasks file

#### Scenario: Author leaves a side-effecting task unmarked

- **WHEN** an unattended-run change has an unmarked task that an operator
  must perform by hand
- **THEN** the reference identifies this as an authoring defect that fails
  the run before archive

### Requirement: Repository documentation points plan authoring at the shared reference

Every repository document, skill package, or plugin README that describes
writing plan documents SHALL direct readers to the client-neutral
`core/plan-authoring.md` reference rather than restating the compile
convention or omitting the authoring path. Repository documentation SHALL
describe plan-level orchestration (`opsx-plan` / `opsx-run`) and SHALL route
per-change propose, apply, archive, and verify work to upstream OpenSpec rather
than teaching a controller-owned per-change workflow.

#### Scenario: Authoring documentation points at the reference

- **WHEN** a reader consults a repository document that covers plan authoring
- **THEN** it points at `core/plan-authoring.md` as the source of the
  convention

#### Scenario: Division of labor is documented

- **WHEN** a reader consults the controller documentation for how per-change
  work is performed
- **THEN** the documentation directs them to upstream OpenSpec commands and
  skills for propose, apply, archive, and verify, and reserves `opsx-plan` /
  `opsx-run` for plan-level orchestration and the supported single-change loop

### Requirement: Plan frontmatter declares human gates

The plan-authoring reference and every adapter-facing plan-authoring surface
SHALL document an optional `human_gates` frontmatter field whose value is a list
of exact change slugs declared in the same plan.

The field SHALL mean: the listed changes are operator-gated for a model-level
plan run and are not started by `opsx-orchestrator` without new operator
authority. It SHALL be distinct from `pause_before`:

- `pause_before` is a compiled-manifest gate consumed by the deterministic
  `opsx-plan` runner and is placed through the `## Suggested Manual Gates`
  section;
- `human_gates` is frontmatter read directly by the model-level orchestrator and
  does not add a TOML manifest key.

An absent or empty `human_gates` field SHALL mean no human gates. A value that
names a change not present in the plan SHALL be an authoring defect, not a
silently ignored entry. An explicit `HUMAN_GATES` invocation value is a runtime
override documented by the authoring surface but is not part of the plan
document.

The `/opsx-plan` authoring command SHALL instruct the author to declare
`human_gates` when the plan contains operator-gated changes, and SHALL require
the listed slugs to be verified against the plan’s `### Change:` headings.

#### Scenario: A plan declares its operator-gated change
- **WHEN** a plan contains changes one through four and change four requires an
  operator decision before it may start
- **THEN** its frontmatter declares `human_gates:` with the exact slug of change
  four, and the authoring surface explains that the model orchestrator stops
  before it

#### Scenario: A plan omits the field
- **WHEN** a plan has no operator-gated changes and omits `human_gates`
- **THEN** the field resolves to the empty list and every planned change is
  treated as orchestrator-owned

#### Scenario: An unknown slug is an authoring defect
- **WHEN** `human_gates` names a slug that does not match any `### Change:`
  heading in the plan
- **THEN** the authoring surface and the orchestrator treat it as a fail-closed
  error rather than ignoring it

#### Scenario: Human gates and pause_before are distinguished
- **WHEN** an author reads the frontmatter and manual-gate guidance
- **THEN** the reference explains that `human_gates` is a model-run stop list
  while `pause_before` is a compiled-manifest approval gate, and that neither
  substitutes for the other

#### Scenario: The authoring command requires the field to be considered
- **WHEN** a model authors a plan through `/opsx-plan`
- **THEN** the command instructs it to declare `human_gates` when applicable,
  verify every slug against the plan, and report the declared gates in its final
  response

#### Scenario: Explicit override is documented as runtime input
- **WHEN** an operator needs to clear or replace a plan’s gates for one run
- **THEN** the authoring surface documents the explicit `HUMAN_GATES` input as a
  runtime override rather than instructing the author to edit the plan

### Requirement: Authors size changes and may preauthorize exact split replacements

The plan-authoring reference and every adapter-facing plan-authoring surface
SHALL direct authors to size each change for one implement-review-archive loop
before implementation and to record that sizing basis in the plan or the
`opsx-orchestrator` run.

The reference and `/opsx-plan` command SHALL document an optional
`split_authorizations` frontmatter list. Each entry SHALL name exactly one parent
change, two or more exact child slugs, each child's dependencies, an obligation
map that assigns every parent obligation to exactly one child, a non-empty
aggregate exit proving the parent's original success parameters after all
children archive, and an optional exact donor-path rehome map. The surfaces
SHALL require the author to verify that the parent and child slugs do not
collide with active or archived changes and that the split preserves the phase,
acceptance tier, gates, and human boundary.

The surfaces SHALL state that `split_authorizations` is model-level plan data
read by `opsx-orchestrator`: it adds no compiled-manifest key, the deterministic
`opsx-plan` compiler does not read it, and authors SHALL NOT express a split as
conditional prose or conditional DAG nodes. When selected, the tracked plan SHALL
contain only the chosen nodes.

The `/opsx-plan` command SHALL report the declared split authorizations, or that
the plan declares none, in its final response and SHALL stop treating an
authorization as valid if any slug or obligation mapping is defective.

#### Scenario: An author sizes a change before implementation
- **WHEN** an author writes a change that touches several orthogonal concerns
- **THEN** the authoring surface directs them either to narrow the change to one
  loop or to declare an exact preauthorized split with child scopes

#### Scenario: An author declares an exact replacement
- **WHEN** the author intends a parent change to be replaceable by two named
  child changes
- **THEN** the frontmatter names the exact parent, exact children, dependency
  map, obligation map, and aggregate exit

#### Scenario: An author uses conditional prose
- **WHEN** a plan contains prose such as "split this if it gets hard" without
  exact child slugs, obligations, and an aggregate exit
- **THEN** the authoring surface rejects it as conditional prose rather than a
  valid authorization

#### Scenario: Split authorizations add no compiler surface
- **WHEN** an author reads the frontmatter and compile guidance
- **THEN** the surface explains that `split_authorizations` is read only by the
  model-level orchestrator and adds no TOML manifest key or conditional node

#### Scenario: The authoring command reports the declarations
- **WHEN** a model authors a plan through `/opsx-plan`
- **THEN** the command instructs it to validate every split authorization and
  report the declared replacements, or that the plan declares none

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
