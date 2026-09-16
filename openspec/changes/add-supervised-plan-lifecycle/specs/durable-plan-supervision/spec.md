## ADDED Requirements

### Requirement: Supervised job registration persists the complete job record

The system SHALL provide a supervised job registration that persists, in one
durable transaction: the protected manifest snapshot and its hash, the
repository root and worktree identity, the standing permissions as the
authority configuration, the frozen model selection and inexpensive
allowlist together with the budgets and deadlines as the protected job
policy at operator revision 1, and the primary session linkage
configuration. The registration SHALL be durable before the job can be
started: a lifecycle command targeting a job that was never registered SHALL
be refused with a named unknown-job error.

Registration SHALL fail closed: on a host without a supported isolation
backend it SHALL be refused with the named unsupported-host error and SHALL
record nothing. Registration SHALL record nothing outside the trusted
service-owned storage: no supervised registration state SHALL be written to
the worktree or the JSON execution state.

#### Scenario: Registration persists the full record

- **WHEN** an operator registers a plan as a supervised job on a supported
  host and the ledger is reopened
- **THEN** the job record carries the repository root and worktree identity,
  the protected manifest snapshot and its hash, and the primary session
  linkage configuration, and the protected job policy at operator revision 1
  carries the authority configuration, the frozen model selection and
  allowlist, and the budgets and deadlines

#### Scenario: Registration fails closed on an unsupported host

- **WHEN** registration is attempted on a host without a supported isolation
  backend
- **THEN** it is refused with the named unsupported-host error, no job
  record is created, and no registration state appears in the worktree

#### Scenario: A lifecycle command on an unregistered worktree is refused

- **WHEN** `start`, `resume`, `pause`, `drain`, or `cancel` targets a
  worktree with no registered supervised job
- **THEN** the command fails with a named unknown-job error and no job
  record is created

### Requirement: The lifecycle commands drive the supervised job state machine

A supervised job SHALL follow the state machine `registered → active →
(paused → active)* → completed | failed | cancelled`, with `completed`,
`failed`, and `cancelled` terminal. Every transition SHALL be a durable
ledger transaction.

`start` SHALL transition a `registered` job to `active`, bringing up the
service-owned execution and the primary session. `resume` SHALL transition a
`paused` job back to `active` only after resume revalidation confirms every
relied-upon receipt still matches the current material revision; a gate
whose receipt no longer matches SHALL return to awaiting its defined
authority instead of dispatching.

A transition outside the legal set SHALL be refused with a named
illegal-transition error, and a mutating lifecycle verb targeting a terminal
job SHALL be refused with a named terminal-job error. Neither refusal SHALL
alter the job record.

#### Scenario: Start activates a registered job

- **WHEN** `start` runs for a `registered` job
- **THEN** the job transitions to `active`, the service-owned execution and
  the primary session come up, and the transition is durable across a ledger
  reopen

#### Scenario: Resume revalidates before activating

- **WHEN** `resume` runs for a `paused` job whose relied-upon receipts all
  match the current material revision
- **THEN** the job transitions to `active` and dispatch proceeds using those
  receipts

#### Scenario: Resume with a stale receipt re-arms the gate

- **WHEN** `resume` runs for a `paused` job and an explicit policy revision
  has invalidated a relied-upon receipt
- **THEN** the affected change returns to awaiting its approval authority
  and is not dispatched

#### Scenario: An illegal transition is refused

- **WHEN** a lifecycle verb requests a transition outside the legal set —
  for example `resume` on an `active` job or `start` on a `paused` job
- **THEN** the command fails with a named illegal-transition error and the
  job state is unchanged

#### Scenario: A terminal job refuses mutation

- **WHEN** a mutating lifecycle verb targets a `completed`, `failed`, or
  `cancelled` job
- **THEN** it is refused with a named terminal-job error and the job record
  is unchanged

### Requirement: Pause and drain are explicit stop boundaries with defined in-flight disposition

`pause` and `drain` SHALL each record a durable stop request for the job,
observed at the dispatch boundary before any new side effect. From the
boundary, no new action SHALL be dispatched. The stop request SHALL survive
restarts: a job that restarts between the request and its observance SHALL
still honor it. Recording a stop request SHALL NOT require acquiring the
worktree execution lock, and the request SHALL wake a waiting job through
the same durable receipt mechanism as approval receipts.

The two boundaries differ in in-flight disposition. Under `pause`, in-flight
actions SHALL be interrupted and marked `uncertain` for evidence
reconciliation, and the job SHALL enter `paused`. Under `drain`, in-flight
actions SHALL run to a terminal outcome while no new dispatch begins, and
only then SHALL the job enter `paused`.

#### Scenario: Pause interrupts in-flight work

- **WHEN** `pause` is recorded while an action is in flight
- **THEN** no new action is dispatched, the in-flight action is interrupted
  and marked uncertain for later reconciliation, and the job enters `paused`

#### Scenario: Drain lets in-flight work finish

- **WHEN** `drain` is recorded while an action is in flight
- **THEN** no new action is dispatched, the in-flight action runs to a
  terminal outcome, and the job enters `paused` only afterward

#### Scenario: A stop request survives a restart

- **WHEN** a stop request is recorded and the job's execution restarts
  before observing it
- **THEN** the restarted execution observes the durable request at its
  dispatch boundary and honors it before dispatching any new action

#### Scenario: A stop request needs no execution lock

- **WHEN** a pause or drain request is recorded while the job waits on a
  human-only approval and holds no execution lock
- **THEN** the request is durably recorded and the job is woken to observe
  it, without any lock acquisition

### Requirement: Cancellation is a terminal transaction with explicit effects

`cancel` SHALL record the `cancelled` terminal state in one durable
transaction. In-flight actions SHALL be failed with a cancellation reason,
or marked `uncertain` where the outcome cannot be confirmed, under the
journal's existing semantics. Cancellation SHALL terminate the job's
ownership: a new registration for the same worktree SHALL become legal once
the prior job is `cancelled`, under the single-supervised-job-per-worktree
invariant.

A cancelled job SHALL NOT be resumable: every later receipt, stop request,
or lifecycle verb for the job SHALL be refused with a named terminal-job
error, and no transition out of `cancelled` SHALL exist.

#### Scenario: Cancel records the terminal state with in-flight disposition

- **WHEN** `cancel` runs for a non-terminal job with an action in flight
- **THEN** the job records `cancelled` durably, and the in-flight action is
  failed with a cancellation reason or marked uncertain for reconciliation

#### Scenario: A new registration is legal after cancellation

- **WHEN** a job for a worktree has reached `cancelled` and the operator
  registers a new supervised job for the same worktree
- **THEN** the new registration succeeds and the cancelled job's record is
  unchanged

#### Scenario: A cancelled job refuses all further requests

- **WHEN** an approval receipt, a stop request, or a lifecycle verb targets
  a `cancelled` job
- **THEN** it is refused with a named terminal-job error and nothing is
  recorded

### Requirement: A human wait is durable job state with receipt-driven wake-up

When a supervised job reaches a human-only gate, the wait SHALL be persisted
as normal durable job state carrying the awaiting checkpoint, the material
revision, and the wait start, and SHALL survive restarts. During a human
wait the job SHALL retain permanent ownership without holding the worktree
execution lock.

There SHALL be no LLM polling and no stall recovery for a human wait: the
job SHALL dispatch no model action while the wait lasts, and no automated
recovery SHALL fire on its account. The wait SHALL consume no execution
deadline or elapsed budget, under the existing deadline-separation
requirement. The job SHALL wake through the durable receipt scan, and
resuming after the wait SHALL revalidate receipts before dispatch.

#### Scenario: The wait is durable across restarts

- **WHEN** a supervised job records a human wait and the service restarts
  before any approval arrives
- **THEN** the reopened job still carries the wait with its checkpoint,
  material revision, and start, and dispatches no model action for it

#### Scenario: No polling during a human wait

- **WHEN** a supervised job is waiting on a human-only approval
- **THEN** it issues no model dispatch and runs no stall recovery while the
  wait lasts, however long it lasts

#### Scenario: An approval receipt wakes the job

- **WHEN** the operator's approval receipt is recorded while the job waits
- **THEN** the job is woken through the durable receipt scan without the
  execution lock, revalidates the receipt against the current material
  revision, and resumes dispatch

### Requirement: Supervised completion is verified from plan, archive, and check evidence

A supervised job SHALL reach `completed` only when every enabled change in
the plan is verified done from the existing ground truth: archive evidence
per change, the post-archive fast checks, and post-archive cleanliness —
never from a worker or primary session's claim of done. A completion claim
without that evidence SHALL NOT complete the job.

When completion evidence needs revalidation — a failed post-archive fast
check or an unverified or partial archive — the lifecycle SHALL rerun an
appropriate fresh review through the existing implement/review/archive loop
rather than treating a prior archive as proof of done. Revalidation SHALL be
bounded by the existing round budgets, so it cannot loop without limit.

Completion of non-supervised runs SHALL be determined exactly as before.

#### Scenario: Completion only from evidence

- **WHEN** every enabled change in a supervised job's plan is verified done
  from archive evidence with the post-archive fast checks and cleanliness
  passing
- **THEN** the job reaches `completed`

#### Scenario: A worker claim does not complete the job

- **WHEN** a worker or the primary session reports the plan done but archive
  evidence or fast checks do not verify
- **THEN** the job does not reach `completed`, and the unverified change
  continues through the existing loop

#### Scenario: A failed fast check triggers fresh review

- **WHEN** a post-archive fast check fails or an archive cannot be verified
  for a change in a supervised job
- **THEN** the lifecycle reruns a fresh review for that change through the
  existing loop instead of treating the prior archive as proof of done, and
  repeated failure is bounded by the change's round budget

#### Scenario: A legacy run's completion is unchanged

- **WHEN** an ordinary, unregistered plan run finishes
- **THEN** its completion is determined exactly as before this change, with
  no supervised completion logic involved

### Requirement: The implement/review/archive loop remains the progression authority

The lifecycle surface SHALL drive progression only through the existing
implement/review/archive loop and its gates: `pause_before` approval gates,
`review_created` acceptance, and the task-completeness gates SHALL apply
exactly as they do for the run engine today. The lifecycle SHALL introduce
no new DAG, stage machine, or dispatcher, and no lifecycle command SHALL
mark a change done, release a gate, or satisfy a checkpoint outside the
loop's existing authorities.

#### Scenario: A gated change still awaits its approval authority

- **WHEN** a supervised job reaches a change gated with `pause_before =
  true`
- **THEN** the gate is released only through the approval authority the
  broker enforces for it, exactly as for the unsupervised engine

#### Scenario: A lifecycle command cannot progress a change

- **WHEN** any lifecycle command other than the loop's own dispatch runs for
  a supervised job
- **THEN** no change is marked done, no gate is released, and no checkpoint
  is satisfied by that command
