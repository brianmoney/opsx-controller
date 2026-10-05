# task-completeness-gates Specification

## Purpose

Ensures an `opsx-plan` run never reaches archive with incomplete automatable
work, while giving operator-only manual-verification tasks a first-class
marker so they no longer trap a run in an implement-review-archive failure
loop.

## Requirements

### Requirement: Manual task marker convention

A task line in a change's `tasks.md` whose text ends with the marker
`(manual)` (case-insensitive) SHALL be classified as an operator-only manual
task. Any other task line SHALL be classified as automatable. Task counting
and completeness gates across the controller and all worker prompts SHALL
use this same classification, so a task is treated identically at implement,
review, and archive time.

#### Scenario: Marked task is manual

- **WHEN** a tasks file contains `- [ ] 4.2 Plant a malformed artifact and
  run the live jobs (manual)`
- **THEN** task 4.2 is classified as manual everywhere task completeness is
  evaluated

#### Scenario: Unmarked task is automatable

- **WHEN** a tasks file contains `- [ ] 1.3 Add a regression test`
- **THEN** task 1.3 is classified as automatable everywhere task
  completeness is evaluated

### Requirement: Controller gates implement advancement on task completeness

When an implement worker returns `status=implemented` with one or more
unchecked automatable tasks remaining, the controller SHALL NOT advance the
change to review. The controller SHALL re-enter implement with a corrective
prompt naming the remaining automatable task ids, consuming the change's
normal round budget, and SHALL fail the change with a reason naming the
remaining task ids when the budget is exhausted. When every remaining
unchecked task is manual, the controller SHALL advance to review normally.

An explicitly accepted deferral SHALL be reflected in the current agreed
scope/specs and retained as an identified plain follow-up entry rather than an
active task checkbox. The entry SHALL retain the requirement or task id, reason,
impact, follow-up, and accepting agreement/scope reference. Such a scoped-out
follow-up SHALL NOT count as an unchecked automatable task or force a retry
solely because its work is unimplemented. Workers SHALL NOT invent acceptance,
check deferred work as completed, or misuse `(manual)` to evade these gates.
Any still-active unchecked automatable task SHALL continue to block advancement.

#### Scenario: Implemented with automatable tasks remaining

- **WHEN** implement returns `status=implemented` and unchecked automatable
  tasks remain in the change's tasks file
- **THEN** the change re-enters implement with a corrective prompt naming
  those task ids instead of advancing to review

#### Scenario: Round budget exhausted with automatable tasks remaining

- **WHEN** implement keeps returning `status=implemented` with unchecked
  automatable tasks until the round budget is exhausted
- **THEN** the controller fails the change with a reason naming the
  remaining task ids

#### Scenario: Only manual tasks remaining

- **WHEN** implement returns `status=implemented` and every unchecked task
  in the change's tasks file is marked manual
- **THEN** the change advances to review in the same round

#### Scenario: Accepted scoped-out follow-up does not consume a round

- **WHEN** every active automatable task is checked and an explicitly accepted nonessential deferral is recorded as an identified plain follow-up with the current scope/specs reconciled
- **THEN** the completeness gate advances to review without a retry for that follow-up
- **AND** the deferred work is disclosed as unimplemented rather than counted as completed or manual

#### Scenario: A deferral label does not waive an active task

- **WHEN** an active automatable task remains unchecked even though its text or worker summary labels it deferred
- **THEN** the task-completeness gate still prevents advancement

### Requirement: Reviewer enforces task completeness

When the reviewer input reports fewer complete tasks than total tasks, the
reviewer SHALL inspect the change's tasks file and SHALL return
`verdict=fail` with a blocking finding per unchecked automatable task,
citing the tasks file as locus. Unchecked tasks marked manual SHALL NOT
produce findings on their own. A reviewer SHALL NOT return `verdict=pass`
while unchecked automatable tasks remain.

The reviewer SHALL assess unimplemented requirements by impact against the
current agreed acceptance scope. A gap SHALL block acceptance when essential to
that scope, correct operation of implemented features, or a required security or
correctness guarantee. An explicitly accepted deferral outside those obligations
SHALL be disclosed in the existing result summary or artifact references, not
as a `critical`, `warning`, or `note` finding or corrective `fix_prompt`. A
deferral label alone SHALL NOT establish acceptance or waive a task gate.

#### Scenario: Incomplete automatable task fails review

- **WHEN** the reviewer input header reports `TASK_COUNTS: 8/9` and the one
  unchecked task is not marked manual
- **THEN** the reviewer returns `verdict=fail` with a finding naming the
  unchecked task

#### Scenario: Only manual tasks unchecked passes the completeness gate

- **WHEN** the reviewer input header reports `TASK_COUNTS: 8/9` and the one
  unchecked task is marked manual
- **THEN** the unchecked task alone does not cause a failing verdict

#### Scenario: Accepted deferral is disclosed without failing review

- **WHEN** a nonessential requirement is explicitly accepted for deferral with reason, impact, follow-up, and acceptance reference and the current scope/specs/tasks reflect that agreement
- **THEN** the reviewer discloses it without adding it to `finding_counts`, `findings`, or `fix_prompt`
- **AND** the deferral alone does not force another implementation round

#### Scenario: Required guarantees cannot be deferred by a worker label

- **WHEN** the implementation lacks behavior essential to the agreed acceptance scope or a claimed security/correctness guarantee
- **THEN** the reviewer records the missing accepted-scope behavior as a blocking finding even if the worker calls it deferred

### Requirement: Archiver exempts manual tasks and reports an operator checklist

The archiver's fail-closed unchecked-task gate SHALL apply only to
automatable tasks: an unchecked task marked manual SHALL NOT block archive.
When a change is archived with pending manual tasks, the archive result
SHALL surface those tasks to the operator as a post-archive checklist. An
unchecked automatable task SHALL still block archive with a blocked result
whose retry outlook reflects that a content change is required.

#### Scenario: Manual-only remainder archives cleanly

- **WHEN** the archiver runs for a change whose only unchecked tasks are
  marked manual
- **THEN** the archive proceeds and its result lists the pending manual
  tasks as an operator checklist

#### Scenario: Unchecked automatable task still blocks archive

- **WHEN** the archiver runs for a change with an unchecked task not marked
  manual
- **THEN** the archiver returns a blocked result naming the unchecked task

### Requirement: Implementer reports verified round progress without self-certifying change completeness

The implementer worker contract SHALL define `status=implemented` as reporting
verified work completed in the current round, even when additional active
automatable tasks remain. It SHALL report the true remaining tasks and counts
and SHALL NOT check a task until its required work is implemented and supported
by applicable evidence. The controller SHALL independently apply the existing
task-completeness gate before advancing to review.

The implementer SHALL report `status=blocked` with a reason when a hard blocker
stops further progress, rather than merely because remaining work does not fit
in one round. Manual tasks MAY remain unchecked. Explicitly accepted scoped-out
deferrals SHALL be recorded as plain follow-ups and disclosed honestly; they
SHALL NOT be represented as completed work.

#### Scenario: A round completes verified partial work

- **WHEN** an implement worker completes and verifies its planned work for the round and further active automatable tasks remain
- **THEN** it may return `status=implemented` with those tasks in `remaining_tasks` and accurate counts
- **AND** the controller still prevents review advancement until active automatable work is complete

#### Scenario: A hard blocker prevents further implementation

- **WHEN** an unclear requirement, conflicting handoff, or unworkable environment stops further progress
- **THEN** the implementer returns `status=blocked` naming that blocker

#### Scenario: Manual task left pending

- **WHEN** an implement worker completes all automatable tasks and only a
  manual task remains unchecked
- **THEN** it returns `status=implemented` with the manual task listed in
  `remaining_tasks`

### Requirement: Supervised repair verdicts never self-certify completion

A `fixer` report of repaired SHALL NOT by itself mark any work complete:
the independent `verifier` SHALL validate the actual diff and evidence
before any authorized commit, reset, or resume consumes the repair. The
`verifier` verdict SHALL be derived from the artifacts and diff under
inspection, never from the fixer session's own account. Supervised repair
verdicts SHALL feed, but never replace, the existing implement, review, and
archive task-completeness gates: an unchecked automatable task SHALL remain
blocking regardless of any fixer or verifier verdict.

#### Scenario: A fixer claim is validated before it is consumed

- **WHEN** a `fixer` session reports a repair complete
- **THEN** the repair is not consumed by any commit, reset, or resume until
  the independent `verifier` validates the actual diff and records its
  verdict

#### Scenario: A contradicting verifier blocks the repair

- **WHEN** the `verifier` examines a fixer's diff and finds the repair
  incorrect or incomplete
- **THEN** its verdict records the failure and the repair is not treated as
  complete, regardless of the fixer's report

#### Scenario: Repair verdicts do not satisfy the task-completeness gates

- **WHEN** a change has unchecked automatable tasks and a supervised repair
  verdict exists
- **THEN** the existing implementer, reviewer, and archiver completeness
  gates still apply exactly as before, and the repair verdict neither checks
  a task nor waives a gate

#### Scenario: Every repair-consuming transition consumes the gate

- **WHEN** a resume, dispatch, service reset, or delegated release would
  consume a recorded repair
- **THEN** the transition reads the change's latest recorded fixer report and
  verifier verdict, and is refused with a recorded `policy_violation` unless
  an independent verifier verdict reviewed the real diff

### Requirement: Supervised completion reports pending manual tasks as an operator checklist

When a change in a supervised job completes with pending `(manual)` tasks,
the lifecycle and reporting surfaces SHALL present those tasks to the
operator as a checklist and SHALL NOT mark the change, the job, or the run
incomplete or failed on their account. A supervised job whose automatable
work is archived and verified SHALL reach its `completed` state with the
pending manual tasks attached as the operator checklist.

This extends the existing manual-task gate exemptions to the supervised
completion surface: it SHALL NOT relax the implement, review, or archive
task-completeness gates, which continue to apply to automatable tasks
exactly as before.

#### Scenario: A manual-only remainder completes with a checklist

- **WHEN** a supervised job's change finishes with every automatable task
  checked and one or more `(manual)` tasks pending
- **THEN** the change completes, the job can reach `completed`, and the
  pending manual tasks are reported as the operator checklist

#### Scenario: Pending manual tasks never fail the job

- **WHEN** a supervised job's completion is evaluated with pending
  `(manual)` tasks and no other outstanding work
- **THEN** the job is not marked incomplete or failed on account of those
  tasks

#### Scenario: The checklist is visible on the supervised surfaces

- **WHEN** an operator inspects a supervised job that completed with pending
  manual tasks
- **THEN** the pending `(manual)` tasks are shown as the operator checklist

### Requirement: Acceptance verdicts never self-certify completion

An acceptance `accept` verdict SHALL NOT mark any task complete, release
archive on its own, or waive the implement, review, or archive
task-completeness gates. Those gates SHALL continue to apply to automatable
tasks exactly as before, and an unchecked automatable task SHALL remain
blocking regardless of any acceptance verdict. A repair consumed by the
acceptance fix route SHALL be validated by the independent verifier against
the actual diff; a fixer's own account SHALL NOT check a task or satisfy
acceptance. The acceptance stage gates advancement between review and archive;
it is not a task-completeness authority.

#### Scenario: An accept verdict does not check a task

- **WHEN** a change holds an acceptance `accept` verdict and an unchecked
  automatable task remains
- **THEN** the task remains unchecked and the existing implement, review, and
  archive completeness gates still apply

#### Scenario: Unchecked automatable work still blocks

- **WHEN** a change reaches the acceptance stage with unchecked automatable
  tasks
- **THEN** an `accept` verdict does not waive the blocking incompleteness

#### Scenario: A fix consumed by acceptance is independently verified

- **WHEN** the acceptance fix route applies a fixer repair
- **THEN** the repair does not satisfy acceptance or check any task until the
  independent verifier validates the actual diff
