# plan-authoring-guidance Delta

## ADDED Requirements

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
