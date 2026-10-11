# per-change-conclusion-command Specification

## Purpose

Define `/opsx-ship`, a client-neutral command that drives exactly one OpenSpec
change to conclusion in a single invocation: it accepts an optional
operator-supplied ordered step list, sequences the upstream OpenSpec lifecycle,
and enforces sub-agent delegation for every unit of work.

## Requirements

### Requirement: `/opsx-ship` resolves one change and an ordered step list

The command SHALL accept exactly one OpenSpec change id as its first non-empty
input token and SHALL accept an optional inline `STEPS:` block containing a
numbered, ordered list of operator-supplied steps.

When the `STEPS:` block is present, its steps SHALL be authoritative for the
order and scope of the run. When the block is absent, the ordered scope SHALL be
the change's authored `tasks.md` entries in file order.

The command SHALL reject a missing, empty, or multi-change input before
dispatching any sub-agent, and SHALL fail closed — naming the conflicting
artifact — when an operator step contradicts the change's live authored
artifacts, rather than silently widening, narrowing, or reordering authored
scope.

#### Scenario: Change id with inline steps

- **WHEN** an operator invokes `/opsx-ship add-auth` followed by a `STEPS:` block listing three ordered steps
- **THEN** the command resolves `add-auth` as the single change and treats the three listed steps as the authoritative run order

#### Scenario: Change id without steps uses the task list

- **WHEN** an operator invokes `/opsx-ship add-auth` with no `STEPS:` block and the change has an authored `tasks.md`
- **THEN** the command uses the `tasks.md` entries in file order as the run scope

#### Scenario: Missing or multi-change input is rejected

- **WHEN** an operator invokes `/opsx-ship` with no change id, or names two change ids
- **THEN** the command stops with a usage error and dispatches no sub-agent

#### Scenario: A conflicting step fails closed

- **WHEN** an operator step requires work that contradicts the change's current authored tasks or spec deltas
- **THEN** the command stops and reports the contradiction instead of editing the change to fit the step

### Requirement: Every unit of work is dispatched to a sub-agent

The command SHALL run as a conductor: it SHALL NOT edit files, author or amend
change artifacts, write or modify source code, run tests, or perform
implementation work itself. Every unit of work SHALL be dispatched to a
sub-agent through the host client's task or sub-agent facility.

Artifact authoring, implementation, verification, review, and archive work SHALL
each be delegated to a sub-agent, using the upstream OpenSpec skills
(`openspec-ff-change`, `openspec-propose`, `openspec-continue-change`,
`openspec-apply-change`, `openspec-verify-change`, `openspec-archive-change`) and
this repository's `opsx-implementer`, `opsx-reviewer`, and `opsx-archiver`
workers where those contracts apply.

Independent steps SHALL be dispatched together in one message so they run
concurrently, and the conductor SHALL retain only the decisions, gate results,
and synthesis in its own context.

The command SHALL fail closed with a client-specific diagnostic when the host
client cannot dispatch sub-agents, and SHALL NOT fall back to performing the
work inline. The final report SHALL list the sub-agents dispatched, so the
operator has evidence the mandate held.

#### Scenario: Conductor has no authoring capability

- **WHEN** an operator invokes `/opsx-ship` on a client that supports the surface
- **THEN** the conductor cannot edit or write files and every artifact or code change is performed by a dispatched sub-agent

#### Scenario: Independent steps fan out

- **WHEN** the resolved steps contain two changes that do not depend on each other
- **THEN** the conductor dispatches both sub-agents in one message rather than serializing them

#### Scenario: Client without sub-agents fails closed

- **WHEN** the host client cannot dispatch a suitable sub-agent, for example Codex CLI, which cannot dispatch a separately writable sub-agent beneath a read-only conductor
- **THEN** the command stops and reports the missing capability instead of doing the work itself

### Requirement: The command sequences the upstream OpenSpec lifecycle to conclusion

The command SHALL drive the change through the ordered lifecycle: resolve
current state, author any missing required artifacts, validate, implement the
pending tasks, verify, review, and archive — in that order, stopping at
conclusion or at the first fail-closed gate.

Store selection SHALL be discovered once, during the resolve stage, by running
`openspec store list --json`: the command SHALL select exactly one store id
when the change lives in a registered store, thread the same `--store <id>` flag
through every root-aware OpenSpec command (`new change`, `status`,
`instructions`, `list`, `show`, `validate`, `archive`, `doctor`, `context`, and
`view`) and through every authoring and verification delegate for the entire
lifecycle, and SHALL fail closed rather than guess when more than one store
could own the change. The selected store id SHALL NOT change mid-run.

The command SHALL derive the required artifact set from the `requires` edges of
the transitive closure of `applyRequires`, not from artifact file existence, and
SHALL NOT create an artifact whose status is `skipped`.

#### Scenario: Un-authored change is authored before implementation

- **WHEN** the target change does not yet exist or is missing required artifacts
- **THEN** the conductor delegates artifact authoring until the required set is done or skipped before any implementation step is dispatched

#### Scenario: Store flag is threaded

- **WHEN** the change lives in a named OpenSpec store
- **THEN** every `status`, `instructions`, `validate`, and `archive` invocation carries the same `--store <id>` flag

### Requirement: Gates fail closed and the review loop is bounded

The command SHALL treat `openspec validate <change> --strict` as the
post-authoring format gate and SHALL NOT treat it as evidence that tasks are
complete. Task checkboxes SHALL remain the completion gate, with lines ending in
`(manual)` exempt and surfaced to the operator as a checklist.

The command SHALL require a fresh, clean review before archive. Any critical,
warning, or note review finding SHALL loop back through implementation, then
verification, then a **fresh** review — in that order — carrying the complete
corrective handoff, and SHALL be bounded by an explicit finite retry budget.

The retry budget SHALL be conductor-local and in-memory only, with a documented
default of `3`, an optional inline `RETRIES: <n>` input that must be an integer
of at least `1`, a maximum of `10` to which larger parsed values are clamped, an
explicit counter initialized to `0` that is incremented once per corrective
implementation round, and an exhaustion report that names the counter value, the
limit, the limit source, the remaining findings, and the unchecked tasks when
the budget is exhausted. A missing, non-integer, or less-than-one `RETRIES:`
value SHALL be a usage error that dispatches no sub-agent. A stale verification
or a stale review SHALL NOT advance a stage, and verification SHALL NOT
substitute for a fresh review.

The command SHALL require delta-spec synchronization to complete before the
archive move, and SHALL stop without moving the change when a sync is partial or
fails.

#### Scenario: A passing format gate is not task completion

- **WHEN** `openspec validate <change> --strict` passes while the change still has unchecked non-manual tasks
- **THEN** the command does not advance to archive and instead dispatches implementation for the remaining tasks

#### Scenario: Review findings loop back with the handoff

- **WHEN** the delegated reviewer returns a blocking finding and the retry counter is below the limit
- **THEN** the conductor increments the counter and dispatches implementation with the complete corrective handoff, then verification, then a fresh review

#### Scenario: Retry budget is exhausted

- **WHEN** a fresh review returns a blocking finding and the retry counter has reached the limit
- **THEN** the conductor stops and emits the exhaustion report with the counter, the limit, the limit source, the remaining findings, and the unchecked tasks

#### Scenario: Failed sync stops the archive

- **WHEN** delta-spec synchronization into the main specs is partial or fails
- **THEN** the command stops and the change root remains in place so the archive step can be retried

### Requirement: Supported clients install the surface with delegation enforced

The command SHALL be authored once in a client-neutral canonical body and
rendered into each supported client's surface, with a drift check that fails
when a committed surface and the canonical body disagree.

The OpenCode adapter SHALL install an `opsx-ship` conductor agent whose
permissions deny file editing, deny all shell access (no `bash` allowlist), and
allow the task facility, and SHALL install the `/opsx-ship` command routed to
that agent; because the conductor has no shell, every shell command is
delegated. The Claude Code adapter SHALL install the command as a skill whose
allowed tool set excludes the file-editing tools and grants no `Bash` tool at
all, because Claude Code prefix `Bash(...)` rules cannot exclude redirection,
pipelines, or chaining. The Codex CLI adapter SHALL install the command as a
skill that fails closed as unsupported, with a diagnostic naming the missing
capability: Codex CLI cannot dispatch a separately writable sub-agent beneath a
read-only conductor, because spawned agents inherit the parent session's
permission profile and agent roles cannot override sandbox settings. The Claude
Code plugin bundle SHALL ship the namespaced skill.

A client that installs the surface without the delegation-enforcing capability
SHALL be treated as a defect, not as a prompt-only fallback.

#### Scenario: OpenCode conductor denies editing and shell

- **WHEN** the OpenCode adapter installs globally and its `opsx-ship` agent is rendered
- **THEN** the installed agent denies file editing and all shell access, and allows the task facility, so every shell command is delegated

#### Scenario: Claude Code skill cannot edit files or run shell

- **WHEN** the Claude Code adapter installs globally
- **THEN** the installed `opsx-ship` skill's allowed tool set omits the file-editing tools and grants no `Bash` tool at all, so the work must be delegated

#### Scenario: Codex surface fails closed as unsupported

- **WHEN** the Codex CLI adapter installs and `/opsx-ship` is invoked
- **THEN** the skill stops with a diagnostic naming the missing separately-writable-sub-agent capability instead of doing the work inline, and claims no working sandbox escalation

#### Scenario: Canonical body drift fails the check

- **WHEN** a committed client surface no longer matches the canonical body
- **THEN** the generation drift check fails

### Requirement: The command does not replace plan-level orchestration or `opsx-run`

The command SHALL own no durable run state, no phase state machine, and no
durable retry budget; its retry budget exists only in memory for the single
invocation, and it SHALL NOT write orchestrator state or telemetry. It SHALL
stop and surface the failed gate to the operator when the bounded loop's retry
budget is exhausted.

The command SHALL NOT change `opsx-plan`, `opsx-run`, plan manifests, or their
behavior, and SHALL NOT require, read, or create a plan manifest.

The command SHALL be documented as the interactive, model-orchestrated path for
one change, distinct from `opsx-plan` and `opsx-run`, which remain the
deterministic plan-level and single-change controller entrypoints.

#### Scenario: No orchestrator state is written

- **WHEN** a `/opsx-ship` run completes or stops at a gate
- **THEN** no `.opsx-plan/` state, telemetry, or plan manifest is created or modified by the command

#### Scenario: Exhausted loop is surfaced, not silently retried

- **WHEN** the review and implementation loop exhausts its retry budget
- **THEN** the command stops and reports the remaining blocking findings and tasks to the operator
