# durable-plan-supervision Specification

## Purpose

Durable, versioned supervision storage for plan execution: a schema-versioned
SQLite ledger that records supervised jobs, actions, incidents, and protected
job policy in trusted storage outside any writable worktree, so intent and
evidence survive restarts and every later supervision change builds on one
accepted contract.

## Requirements

### Requirement: The supervisor ledger stores jobs, actions, and incidents under a versioned schema

The system SHALL provide a durable supervisor ledger implemented with the
Python standard library only, using SQLite as the storage engine. The ledger
SHALL record supervised jobs, actions, and incidents, and SHALL carry an
explicit schema version stored in the ledger itself.

Opening a ledger at a lower schema version than the current code SHALL migrate
it forward. Schema migrations SHALL be forward-only: opening a ledger whose
recorded schema version is newer than the code supports SHALL fail with a
named error rather than silently downgrading, rewriting, or dropping data.

#### Scenario: A ledger is created and reopened

- **WHEN** a new ledger is created at a trusted location and then closed and
  reopened
- **THEN** the previously recorded job, action, and incident records are
  intact and the recorded schema version matches the code's current version

#### Scenario: An older ledger migrates forward

- **WHEN** a ledger recorded at an older supported schema version is opened by
  newer code
- **THEN** the ledger is migrated to the current schema version, existing
  records are preserved, and the recorded schema version advances

#### Scenario: A newer ledger is rejected

- **WHEN** a ledger whose recorded schema version is newer than the code
  supports is opened
- **THEN** opening fails with a named error and the ledger file is not
  modified

### Requirement: The protected job policy is versioned and revised only explicitly

The ledger SHALL hold a protected job-policy record per supervised job. The
policy record SHALL carry the authority configuration, the model selection and
the inexpensive-model allowlist selection, the hash of the manifest snapshot
the job registered with, the job's budgets and deadlines, and an explicit,
monotonically increasing operator revision.

The policy record SHALL be versioned by its own policy-schema version so later
changes can evolve it under the same forward-only discipline as the ledger
schema. The policy SHALL change only through an explicit operator revision:
an in-place mutation that does not create a new revision SHALL be rejected,
and there SHALL be no silent fallback, inheritance, or defaulting of policy
fields.

#### Scenario: Policy is recorded with its registration fields

- **WHEN** a supervised job is registered
- **THEN** its policy record persists the authority configuration, model and
  allowlist selection, manifest-snapshot hash, budgets, deadlines, the
  policy-schema version, and operator revision 1

#### Scenario: A policy change requires an explicit revision

- **WHEN** a write attempts to change a persisted policy field without
  increasing the operator revision
- **THEN** the write is rejected with a named error and the stored policy is
  unchanged

#### Scenario: An explicit revision supersedes the prior policy

- **WHEN** an operator records a new policy revision
- **THEN** the ledger stores the new revision with an increased revision
  number, and reading the current policy returns the newest revision

### Requirement: Supervision records have durable identities linked to existing runs

Every job, action, and incident SHALL have a unique, durable identity assigned
at creation: a job id, an action id, and an incident id respectively. Action
and incident records SHALL reference their owning job id. Job and action
records SHALL carry the existing `run_id` of the plan run they belong to, so
supervision records link to the existing run schema without redefining it.

#### Scenario: Identities are unique and stable

- **WHEN** jobs, actions, and incidents are created and the ledger is
  reopened
- **THEN** each record retains its original id, no two records of the same
  kind share an id, and each action and incident references its owning job id

#### Scenario: Records link to the existing run

- **WHEN** a job and its actions are recorded for a plan run
- **THEN** they carry that run's `run_id`, and querying by `run_id` returns
  the job and its actions

### Requirement: The ledger lives in trusted storage outside any writable worktree

The ledger SHALL reside in external, service-owned storage outside the
repository and any writable worktree. Configuring a ledger location that
resolves inside the repository or a writable worktree SHALL be rejected with a
named trusted-location error. Path comparisons SHALL be made on canonicalized
paths so a symlink or relative-path spelling cannot smuggle a repo-internal
location past the check.

References from ledger records back into the repository (for example the
manifest snapshot path) SHALL be stored as repository-relative data, with the
repository root recorded separately, so records remain valid if the checkout
moves.

#### Scenario: A repo-internal location is rejected

- **WHEN** a ledger is configured at a path inside the repository or a
  writable worktree, including through a symlinked or relative spelling
- **THEN** creation is refused with a named trusted-location error and no
  ledger file is created at that path

#### Scenario: Repository references are stored as relative data

- **WHEN** a job records a reference to a file in its repository
- **THEN** the ledger stores the repository root and the repository-relative
  path as data rather than an absolute path embedded in the record

### Requirement: The journal records intent before side effects and reconciles evidence

The ledger SHALL journal every supervised action's intent in its own committed
transaction before any side effect of that action is attempted. A dispatch
record, carrying the action id and its owning job, SHALL be recorded
transactionally after the intent. An action whose outcome cannot be confirmed
SHALL be marked explicitly uncertain, and an uncertain action SHALL be
reconciled against recorded evidence before it is completed, failed, or
replayed. `completed` and `failed` SHALL be terminal: a terminal action SHALL
NOT be dispatched or replayed. The journal SHALL NOT claim exactly-once
external effects: replay SHALL deduplicate and re-observe rather than assume a
prior attempt had no effect.

#### Scenario: Intent is durable before the side effect

- **WHEN** a supervised action is begun and the process is interrupted after
  the intent is recorded but before any side effect completes
- **THEN** the reopened ledger contains the committed intent record for that
  action

#### Scenario: An uncertain action requires reconciliation

- **WHEN** an action's outcome cannot be confirmed
- **THEN** the action is marked uncertain, and it cannot be marked complete or
  failed, replayed, or dispatched until evidence reconciling it has been
  recorded

#### Scenario: A failed action is terminal

- **WHEN** an action has failed
- **THEN** dispatch and replay refuse it with a named error and record no new
  dispatch, and only a new action can retry the work

### Requirement: Ledger writes are transactional and survive interruption

Every ledger write that spans multiple records SHALL execute in a single
transaction. An interrupted multi-record write SHALL leave no partial state:
reopening the ledger SHALL expose either the complete write or none of it.

#### Scenario: An interrupted write leaves no partial record

- **WHEN** a simulated interruption kills a multi-record write mid-transaction
  and the ledger is reopened
- **THEN** none of that transaction's records are present, and the ledger is
  otherwise consistent and usable

### Requirement: At most one supervised job owns a worktree

The ledger SHALL enforce a single active supervised job per worktree.
Registering a second supervised job for a worktree that already has an active
job SHALL be refused with a named error.

#### Scenario: A second job for the same worktree is refused

- **WHEN** an active supervised job exists for a worktree and a second
  registration for the same worktree is attempted
- **THEN** the registration fails with a named error and the existing job is
  unchanged

### Requirement: The supervisor ledger does not change legacy execution state

The supervisor ledger SHALL be storage separate from the authoritative JSON
execution state, and introducing it SHALL NOT change the JSON state's
location, schema, or semantics. Operators who never register a supervised job
SHALL observe no behavioral difference and SHALL NOT need the ledger or any
supervision backend for legacy runs.

#### Scenario: A legacy run needs no ledger

- **WHEN** an ordinary, unsupervised `opsx-plan` command runs in a repository
  with no registered supervised job
- **THEN** it completes without opening, creating, or requiring a supervisor
  ledger, and the JSON execution state is handled exactly as before

### Requirement: The supervision contract is documented as a client-neutral reference

The supervision contract SHALL be documented in `core/plan-supervision.md` as
a client-neutral reference covering: the job, action, and incident lifecycles;
journal-before-side-effects ordering; evidence reconciliation; the ownership
fields on each record; the single-supervised-job-per-worktree invariant; and
the trusted-location path semantics consistent with the isolation boundary.

#### Scenario: The reference covers the contract elements

- **WHEN** `core/plan-supervision.md` is reviewed against this capability
- **THEN** every element listed above is defined in it, and the document
  states them without reference to any specific client adapter
