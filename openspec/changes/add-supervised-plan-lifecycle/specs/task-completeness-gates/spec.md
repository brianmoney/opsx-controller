## ADDED Requirements

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
