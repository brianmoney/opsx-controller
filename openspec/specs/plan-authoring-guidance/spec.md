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
