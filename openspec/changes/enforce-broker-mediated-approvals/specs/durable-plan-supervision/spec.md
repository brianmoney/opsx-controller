## MODIFIED Requirements

### Requirement: A gated change's approval authority is human-only unless explicitly delegated

Every change gated with `pause_before = true` SHALL have a defined approval
authority: human-only, meaning only a human operator can release the gate, or
delegated, meaning the supervised job's policy-bound authority may release
it.

The manifest key `pause_before_human_only` SHALL select the approval
authority for a gated change. When the key is absent on a gated change, the
gate SHALL resolve to human-only, so legacy manifests keep their existing
human-approved semantics. An explicit `pause_before_human_only = false`
SHALL delegate approval to the supervised job's policy-bound authority.

`pause_before_human_only = true` SHALL be invalid on a change that is not
gated with `pause_before = true`: a human-only approval requirement is
meaningless without a gate, so the combination SHALL be rejected rather than
silently accepted.

For a registered supervised job, the broker SHALL enforce the resolved
authority at runtime: a human-only gate SHALL be released only by an approval
receipt recorded through the operator OS-authenticated path, and a delegated
gate SHALL be released only by a receipt recorded through the scoped job
service action. Unsupervised, unregistered runs SHALL handle gates exactly as
before.

#### Scenario: Legacy gate resolves to human-only

- **WHEN** a change sets `pause_before = true` and omits
  `pause_before_human_only`
- **THEN** its approval authority resolves to human-only

#### Scenario: Explicit opt-out delegates approval

- **WHEN** a change sets `pause_before = true` and
  `pause_before_human_only = false`
- **THEN** its approval authority resolves to the supervised job's
  policy-bound delegated authority

#### Scenario: Human-only without a gate is invalid

- **WHEN** a change sets `pause_before_human_only = true` without
  `pause_before = true`
- **THEN** the manifest is rejected as invalid rather than silently accepted

#### Scenario: Ungated changes resolve no approval authority

- **WHEN** a change sets neither `pause_before = true` nor
  `pause_before_human_only`
- **THEN** no approval authority applies and the change loads unchanged

#### Scenario: A human-only gate refuses the worker path

- **WHEN** a gated change in a registered supervised job resolves to
  human-only and a release is attempted through the worker-actions endpoint
  or by mutating execution state directly
- **THEN** the gate is not released and the attempt is refused or ignored as
  a non-authoritative write

#### Scenario: A delegated gate releases through the scoped service action

- **WHEN** a gated change in a registered supervised job resolves to
  delegated and the policy-bound job service records the approval through its
  scoped action
- **THEN** the broker records the receipt and the gate is released for the
  bound checkpoint and material revision

## ADDED Requirements

### Requirement: The broker is the sole approval authority for registered supervised jobs

For a registered supervised job, the broker in the trusted authority domain
SHALL be the sole authority that releases approval and acceptance gates.
Direct mutation of the JSON execution state, the plan manifest, or any
repo-writable file SHALL NOT release a gate, satisfy a checkpoint, or alter a
job's supervised identity.

Within a registered job, `approve`, `approve --all`, `approve P<N>`,
`accept`, `reset`, `run`, `run-one`, and `opsx-run` SHALL be broker mediated:
each is recorded as a durable broker transaction or refused, and execution
decisions consult broker state rather than unmediated JSON writes. For
registered jobs the JSON execution state SHALL be a projection of broker and
ledger state, not a competing phase authority.

Unregistered legacy jobs SHALL keep their existing JSON handling with no
dependency on the broker, the supervisor ledger, or any supervision backend.

#### Scenario: A worker write cannot release a gate

- **WHEN** a worker-domain process edits the JSON execution state to add an
  approval for a gated change in a registered supervised job
- **THEN** the gate remains unreleased, because only a broker-recorded
  receipt satisfies it

#### Scenario: An unregistered job needs no broker

- **WHEN** an ordinary `opsx-plan approve` runs in a repository with no
  registered supervised job
- **THEN** it mutates the JSON execution state exactly as before, without
  opening or requiring any broker connection or supervisor ledger

#### Scenario: A stale projection does not mislead the operator

- **WHEN** the JSON projection for a registered job is regenerated from
  broker and ledger state after a receipt is recorded
- **THEN** the projected gate state matches the broker's recorded receipts

### Requirement: Approval and acceptance receipts bind to the exact checkpoint and material revision

Each approval or acceptance receipt SHALL bind to the exact checkpoint (the
specific gated change and gate kind) and to a material revision computed by
hashing only the material gate inputs: the gate-relevant manifest fields for
that change taken from the protected manifest snapshot, the protected
snapshot's identity hash, and the current explicit policy revision.

Unrelated updates — task progress, telemetry, other changes' state, or
non-gate manifest fields — SHALL NOT invalidate a receipt. A receipt recorded
against a different material revision SHALL NOT satisfy the gate.

Plan and policy revisions SHALL be explicit: the material revision changes
only when an operator registers a new protected snapshot or records a new
explicit policy revision, never as a side effect of worker writes to the
repository.

#### Scenario: A stale material revision does not satisfy the gate

- **WHEN** an approval receipt exists for a gated change but the operator has
  since registered a new policy revision or protected snapshot that changes
  the material gate inputs
- **THEN** the prior receipt does not satisfy the gate and the change awaits
  approval again

#### Scenario: An unrelated update does not invalidate a receipt

- **WHEN** an approval receipt exists for a gated change and task progress or
  telemetry for an unrelated change is recorded without any explicit plan or
  policy revision
- **THEN** the receipt continues to satisfy the gate

#### Scenario: Worker edits cannot shift the material revision

- **WHEN** a worker-domain process edits the repo plan file or JSON state in
  a way that would alter gate-relevant fields
- **THEN** the material revision computed from the protected snapshot is
  unchanged and existing receipts keep their defined validity

### Requirement: A protected manifest snapshot and external registration record anchor supervised identity

A registered supervised job SHALL be anchored by two service-owned records
held outside the worktree: the external registration record (the ledger job
and its protected job policy) and a protected manifest snapshot capturing the
manifest content the job was registered with.

Gate and mediation decisions SHALL be evaluated from these protected records,
not from repo-writable copies. A worker that edits the JSON execution state
or the repo plan to drop supervised fields SHALL NOT escape active
registration: the job remains registered and broker mediated until the
registration record itself reaches a terminal state through an authorized
path.

#### Scenario: Dropping supervised fields does not escape registration

- **WHEN** a worker-domain process removes supervised markers from the JSON
  execution state or the repo plan for a registered job
- **THEN** the job is still registered, mutating commands are still broker
  mediated, and the tampered files have no effect on gate authority

#### Scenario: The protected snapshot lives outside the worktree

- **WHEN** a job is registered
- **THEN** the manifest snapshot is stored in service-owned storage outside
  the worktree and is not writable by the worker domain

### Requirement: Resume revalidates material gate inputs before dispatch

Before a supervised job resumes dispatch after a restart, a pause, or a
human wait, the broker SHALL revalidate the material gate inputs for every
gate the job believes is satisfied: each relied-upon receipt SHALL be matched
against the current material revision, and any gate whose receipt no longer
matches SHALL return to awaiting its defined authority.

#### Scenario: Resume with valid receipts dispatches

- **WHEN** a supervised job resumes and every relied-upon receipt matches the
  current material revision
- **THEN** dispatch proceeds using those receipts

#### Scenario: Resume with a stale receipt re-arms the gate

- **WHEN** a supervised job resumes after an explicit policy revision
  invalidated a relied-upon receipt
- **THEN** the affected change returns to awaiting its approval authority and
  is not dispatched

### Requirement: Supervised resets are bounded and worker-initiated resets are refused

Within a registered supervised job, a reset SHALL occur only through an
authorized path: an operator reset recorded through the operator
OS-authenticated path, or a bounded service reset within the job's policy
limits. A reset attempted directly by a worker-domain process — through
`opsx-plan reset`, `opsx-run`, or JSON mutation — SHALL be refused with a
named error and SHALL NOT alter broker or ledger state.

Authorized resets SHALL be recorded as broker transactions so the reset
history is durable and auditable.

#### Scenario: A worker reset is refused

- **WHEN** a worker-domain process runs `opsx-plan reset` for a change in a
  registered supervised job
- **THEN** the command fails with a named error identifying broker mediation
  and no reset occurs

#### Scenario: An operator reset is recorded

- **WHEN** the operator resets a change through the operator path for a
  registered job
- **THEN** the broker records the reset as a durable transaction and the
  change returns to its pre-dispatch gate state

### Requirement: Pause and steer requests are durable broker receipts

Within a registered supervised job, a pause or steer request SHALL be recorded
as a durable broker transaction of kind `pause` or `steer`, bound to the
requesting change's checkpoint and material revision, and SHALL wake the
owning job through the same durable wake-up mechanism as approval receipts. A
pause or steer request SHALL NOT require the held worktree execution lock, and
a worker-domain process SHALL NOT record one except through its scoped service
action.

#### Scenario: A pause request is a durable receipt

- **WHEN** the policy-bound service records a pause request for a change in a
  registered supervised job
- **THEN** the broker records a durable `pause` receipt bound to the change's
  checkpoint and material revision, and the owning job is woken by the receipt
  scan without the execution lock

#### Scenario: A worker cannot record a pause or steer request

- **WHEN** a worker-domain process attempts to record a pause or steer request
  except through its scoped service action
- **THEN** the attempt is refused and no receipt is recorded
