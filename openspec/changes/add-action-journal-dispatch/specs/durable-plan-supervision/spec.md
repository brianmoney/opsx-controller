## ADDED Requirements

### Requirement: Every supervised stage dispatch flows through the journal lifecycle

For a registered supervised job, the run engine SHALL drive every inner stage
dispatch — create, implement, review, and archive, including stage retries and
`implementer_escalation` dispatches — through the full journal lifecycle:
action intent committed in its own transaction before any side effect, a
transactional dispatch record after the intent, and then a terminal outcome
(`completed` or `failed`) or an explicit `uncertain` mark with recorded
evidence. The journal SHALL wrap the existing run engine dispatch path; no new
DAG, stage machine, or replacement dispatcher SHALL be introduced.

A supervised action SHALL NOT be left without an outcome: when the engine
finishes, interrupts, or abandons a dispatch, it SHALL record the outcome or
the uncertainty in the journal before the run continues, pauses, or exits.

For an unregistered legacy run, dispatch SHALL behave exactly as before: no
journal records are created and no journal, policy, or ledger dependency is
introduced.

#### Scenario: An inner stage dispatch is journaled end to end

- **WHEN** a registered supervised job dispatches an implement, review,
  archive, or create stage through the existing run engine
- **THEN** the ledger holds the intent record committed before the stage's
  side effects, a dispatch record bound to that action, and a terminal or
  explicitly uncertain outcome with evidence once the stage resolves

#### Scenario: Every call class is journaled

- **WHEN** a supervised job performs a create-stage dispatch, re-dispatches a
  stage as a retry, or dispatches through `implementer_escalation`
- **THEN** each dispatch is its own journaled action with its own intent,
  dispatch record, and outcome, and no call class runs outside the journal

#### Scenario: A legacy run creates no journal records

- **WHEN** an ordinary, unregistered plan run executes in a repository with
  the supervisor package present
- **THEN** its dispatch path is unchanged and the supervisor ledger contains
  no actions for that run

### Requirement: Supervised dispatch is gated on lock, authority, model policy, and budget before side effects

For a registered supervised job, every action SHALL pass one dispatch boundary
that evaluates, before any side effect of the action: the worktree execution
lock is held by the dispatching process; the broker's authority state permits
the dispatch; the model policy check passes for the action's role against the
job policy's pinned model identity; and the budget reservation for the action
succeeds. A failure at any of the four SHALL block the dispatch with a named
error identifying the failing gate, and no side effect of the action SHALL
occur. A blocked action SHALL be recorded as failed with the gate reason, or
shall leave the job in its prior state with the block surfaced, rather than
running ungated.

The model policy gate SHALL enforce the archived decision vocabulary: a
missing, unallowlisted, or identity-mismatched role blocks with its named
reason, and no silent fallback or inheritance to another model identity is
permitted.

#### Scenario: A model-policy violation blocks dispatch before side effects

- **WHEN** a supervised action's resolved model identity is missing,
  unallowlisted, or mismatched against the pinned job policy
- **THEN** the dispatch is blocked with the named policy reason, the action
  records no dispatch side effects, and no worker process or task is spawned

#### Scenario: Budget exhaustion blocks dispatch at the same boundary

- **WHEN** a supervised action's reservation would exceed a per-action limit
  or arrives with a total limit exhausted
- **THEN** the dispatch is blocked with the named budget-exhaustion state at
  the same dispatch boundary, after the model policy check and before any
  side effect

#### Scenario: The failing gate is identified

- **WHEN** a dispatch is blocked at the boundary
- **THEN** the error names which gate failed — lock, authority, model policy,
  or budget — so the operator can act on the specific blocker

### Requirement: Each action revalidates the immutable job plan and policy before dispatch

Before each supervised action is dispatched, the engine SHALL revalidate that
the manifest snapshot hash recorded at registration still matches the plan on
disk and that the job policy's operator revision is unchanged since the action
was planned. A plan or policy that changed since registration or since the
prior action SHALL block dispatch with a named stale-material error; the
engine SHALL NOT dispatch against stale material and SHALL NOT silently
re-bind the job to the new plan or policy. Adoption of a changed plan or
policy SHALL require an explicit operator revision, after which dispatch
resumes against the new material.

#### Scenario: A policy change between actions blocks the next dispatch

- **WHEN** the job policy's operator revision changes after one action
  completes and before the next is dispatched
- **THEN** the next dispatch is blocked with a named stale-material error
  until the operator explicitly acknowledges the new revision for the job

#### Scenario: A changed plan snapshot blocks dispatch

- **WHEN** the manifest snapshot hash recorded at registration no longer
  matches the plan on disk at dispatch time
- **THEN** the dispatch is blocked with a named stale-material error and the
  action is not begun

### Requirement: Dispatch records carry session and process identity for both worker dispatch paths

Every supervised dispatch record SHALL carry the identity needed to fence and
reconcile the actual worker: the subprocess dispatch path SHALL record the
worker process identity, and the native Task dispatch path inside a worker
session SHALL record the session identity reported through the worker
endpoint. Both paths SHALL write to the same journal with the same lifecycle
and gating, so a supervised dispatch never has a null identity regardless of
which path carried it.

#### Scenario: A subprocess worker dispatch records process identity

- **WHEN** a supervised stage is dispatched as a subprocess
- **THEN** its dispatch record carries the spawned process identity, and a
  later reconciliation can distinguish that exact process from a recycled PID

#### Scenario: A native Task dispatch records session identity

- **WHEN** a worker dispatches a native Task subagent and reports it through
  the worker endpoint
- **THEN** the journal records the Task's session identity against the action
  in the same journal as subprocess dispatches

### Requirement: Unconfirmed outcomes are marked uncertain and reconciled from evidence in the engine

When a supervised action's outcome cannot be confirmed — the worker process is
lost after spawn, a timeout fires after dispatch, a kill's effect is
ambiguous, or result evidence is missing — the engine SHALL mark the action
explicitly `uncertain` rather than guessing success or failure. The engine
SHALL record outcome and usage evidence against the action as it becomes
available, and an uncertain action SHALL be reconciled against recorded
evidence before it is completed, failed, or replayed. A run with an
unreconciled uncertain action SHALL NOT silently progress to subsequent
actions as if the uncertain one had succeeded or never happened.

#### Scenario: A lost worker marks its action uncertain

- **WHEN** a supervised stage's worker process disappears after the dispatch
  record was written and no outcome was recorded
- **THEN** the action is marked uncertain, and the run does not treat the
  stage as complete or failed until evidence reconciles it

#### Scenario: Evidence reconciles an uncertain action

- **WHEN** evidence resolving an uncertain action's outcome is recorded
- **THEN** the action is reconciled and may then transition to a terminal
  state consistent with that evidence

#### Scenario: An unreconciled uncertain action blocks silent progress

- **WHEN** a supervised run resumes while an action remains uncertain and
  unreconciled
- **THEN** the run surfaces the uncertain action as blocking state instead of
  dispatching the next action as though nothing was pending

### Requirement: Replay deduplicates and re-observes without claiming exactly-once effects

Before replaying an uncertain action, the engine SHALL deduplicate against the
prior dispatch — matching the action's recorded identity and any delivered
results — and SHALL re-observe the external state the prior attempt may have
affected. Replay SHALL NOT assume a prior attempt had no effect, SHALL NOT
double-bill or double-apply a duplicate result, and SHALL NOT claim
exactly-once external effects through a lease or any other mechanism.

#### Scenario: A duplicate result is not applied twice

- **WHEN** a prior attempt's result is delivered again before replay
- **THEN** the duplicate is recognized against the recorded dispatch identity
  and reconciles the existing action rather than being billed or applied as a
  new dispatch

#### Scenario: Replay happens only after re-observation

- **WHEN** an uncertain action has no recorded evidence resolving it
- **THEN** the engine re-observes the external state first and replays only
  when that observation shows the prior attempt did not complete the work

### Requirement: Worker evidence and action requests are journaled through the worker endpoint

The worker actions endpoint SHALL back its `record_evidence` and
`request_action` verbs with the supervisor ledger: `record_evidence` SHALL
persist evidence against the referenced action so uncertain actions can
reconcile, and `request_action` SHALL be answered from journaled job state
rather than synthesized ad hoc. Both verbs SHALL remain inside the existing
worker-domain authorization boundary: a caller outside the worker domain SHALL
be denied, and evidence SHALL be accepted only for actions of the job the
worker is bound to.

#### Scenario: Worker evidence reconciles through the endpoint

- **WHEN** a bound worker reports outcome evidence through `record_evidence`
  for one of its job's uncertain actions
- **THEN** the evidence is persisted in the ledger and the action reconciles
  as it would for engine-recorded evidence

#### Scenario: Evidence for another job is refused

- **WHEN** a worker presents evidence referencing an action owned by a
  different job
- **THEN** the write is refused with a named authorization error and the
  target action is unchanged
