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

### Requirement: Reviewer enforces task completeness

When the reviewer input reports fewer complete tasks than total tasks, the
reviewer SHALL inspect the change's tasks file and SHALL return
`verdict=fail` with a blocking finding per unchecked automatable task,
citing the tasks file as locus. Unchecked tasks marked manual SHALL NOT
produce findings on their own. A reviewer SHALL NOT return `verdict=pass`
while unchecked automatable tasks remain.

#### Scenario: Incomplete automatable task fails review

- **WHEN** the reviewer input header reports `TASK_COUNTS: 8/9` and the one
  unchecked task is not marked manual
- **THEN** the reviewer returns `verdict=fail` with a finding naming the
  unchecked task

#### Scenario: Only manual tasks unchecked passes the completeness gate

- **WHEN** the reviewer input header reports `TASK_COUNTS: 8/9` and the one
  unchecked task is marked manual
- **THEN** the unchecked task alone does not cause a failing verdict

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

### Requirement: Implementer reports implemented only when automatable work is complete

The implementer worker contract SHALL define `status=implemented` as
requiring every automatable task in the change's tasks file to be checked.
An implementer that cannot complete an automatable task SHALL report
`status=blocked` with a reason instead of returning `implemented` with
automatable tasks remaining. Manual tasks MAY be left unchecked without
affecting the reported status.

#### Scenario: Automatable task cannot be completed

- **WHEN** an implement worker finishes its round with an automatable task
  it could not complete
- **THEN** it returns `status=blocked` naming the task rather than
  `status=implemented` with the task in `remaining_tasks`

#### Scenario: Manual task left pending

- **WHEN** an implement worker completes all automatable tasks and only a
  manual task remains unchecked
- **THEN** it returns `status=implemented` with the manual task listed in
  `remaining_tasks`
