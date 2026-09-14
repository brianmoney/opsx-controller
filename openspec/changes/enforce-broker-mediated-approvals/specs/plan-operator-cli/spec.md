## MODIFIED Requirements

### Requirement: `opsx-plan` supports batch approval and acceptance gates

The orchestrator SHALL accept `opsx-plan approve --all` and `opsx-plan accept --all` for a resolved plan.

`approve --all` SHALL affect only changes currently awaiting approval.

`accept --all` SHALL affect only changes currently awaiting acceptance.

Each batch command SHALL print the exact change IDs it affected. If no changes match the requested gate state, the command SHALL report that nothing was changed.

Existing single-change `approve <change-id>` and `accept <change-id>` forms SHALL remain supported and unchanged for unregistered legacy jobs. In a registered supervised job, both batch and single-change forms SHALL remain supported but SHALL be broker mediated: affected changes are recorded as durable broker receipts rather than direct JSON mutations.

#### Scenario: `approve --all` approves every change awaiting approval

- **GIVEN** a resolved plan where `change-a` and `change-b` are awaiting approval and `change-c` is already done
- **WHEN** the operator runs `opsx-plan approve --all`
- **THEN** `change-a` and `change-b` transition out of the awaiting-approval state
- **AND** the command output lists exactly `change-a` and `change-b` as affected changes
- **AND** `change-c` is left unchanged

#### Scenario: `accept --all` only affects awaiting-acceptance changes

- **GIVEN** a resolved plan where `change-a` is awaiting acceptance, `change-b` is awaiting approval, and `change-c` is failed
- **WHEN** the operator runs `opsx-plan accept --all`
- **THEN** only `change-a` transitions out of the awaiting-acceptance state
- **AND** the command output lists exactly `change-a` as affected
- **AND** `change-b` and `change-c` are left unchanged

#### Scenario: Batch gate command reports an empty matching set

- **GIVEN** a resolved plan where no changes are awaiting approval
- **WHEN** the operator runs `opsx-plan approve --all`
- **THEN** no plan state changes occur
- **AND** the command output clearly reports that no changes were awaiting approval

#### Scenario: Batch approval in a registered job records broker receipts

- **GIVEN** a registered supervised job where `change-a` and `change-b` are awaiting approval
- **WHEN** the operator runs `opsx-plan approve --all` through the operator path
- **THEN** the broker records one durable approval receipt per affected change, each bound to its checkpoint and material revision
- **AND** the command output lists exactly `change-a` and `change-b` as affected

### Requirement: `opsx-plan reset --failed` resets all failed changes to pending

The orchestrator SHALL accept `opsx-plan reset --failed` for a resolved plan.

`reset --failed` SHALL affect only changes currently in a failed state and SHALL reset each affected change to pending.

The command SHALL print the exact change IDs it reset. If no changes are failed, it SHALL report that nothing was reset.

Existing single-change `reset <change-id>` SHALL remain supported and unchanged.

In a registered supervised job, `reset --failed` SHALL be broker mediated like any other reset: an authorized operator reset is recorded as durable receipts, and a worker-domain `reset --failed` SHALL be refused with the named broker-mediation error and SHALL reset nothing.

#### Scenario: `reset --failed` resets every failed change

- **GIVEN** a resolved plan where `change-a` and `change-b` are failed,
  `change-c` is awaiting approval, and `change-d` is done
- **WHEN** the operator runs `opsx-plan reset --failed`
- **THEN** `change-a` and `change-b` are reset to pending
- **AND** the command output lists exactly `change-a` and `change-b` as reset
- **AND** `change-c` and `change-d` are left unchanged

#### Scenario: Failed reset reports an empty matching set

- **GIVEN** a resolved plan where no changes are failed
- **WHEN** the operator runs `opsx-plan reset --failed`
- **THEN** no plan state changes occur
- **AND** the command output clearly reports that no failed changes were reset

#### Scenario: A worker cannot blanket-reset in a registered job

- **WHEN** a worker-domain process runs `opsx-plan reset --failed` in a
  registered supervised job
- **THEN** the command fails with a named error identifying broker mediation
  and no change is reset

## ADDED Requirements

### Requirement: Gate and mutating commands in a registered supervised job are broker mediated

When a worktree holds a registered supervised job, `opsx-plan approve`
(including `--all` and `P<N>` forms), `opsx-plan accept`, `opsx-plan reset`
(including `--failed`), `opsx-plan run`, `opsx-plan run-one`, and `opsx-run`
SHALL be broker
mediated: gate releases are recorded through the operator OS-authenticated
path or the scoped job service action, and run dispatch is authorized against
broker receipts and the protected job policy rather than unmediated JSON
writes.

A mutating command attempted by a worker-domain process in a registered job —
approving, accepting without authority, resetting, or running outside the
supervised execution — SHALL be refused with a named error identifying broker
mediation, and SHALL NOT alter broker, ledger, or JSON phase authority.

Registration detection SHALL NOT depend on repo-writable files alone: a
worker that edits the JSON state or plan to drop supervised fields SHALL NOT
turn a registered job back into an unmediated one.

Read-only diagnostics (`status`, `logs`, `report`, `doctor`) SHALL remain
available in a registered job without broker mediation and SHALL NOT be
refused.

#### Scenario: A worker cannot approve in a registered job

- **WHEN** a worker-domain process runs `opsx-plan approve` for a gated
  change in a registered supervised job
- **THEN** the command fails with a named error identifying broker mediation
  and no approval is recorded

#### Scenario: A worker cannot run in a registered job

- **WHEN** a worker-domain process runs `opsx-plan run`, `opsx-plan run-one`,
  or `opsx-run` in a registered supervised job outside the supervised
  execution
- **THEN** the command fails with a named error identifying broker mediation
  and no dispatch occurs

#### Scenario: The operator approves through the authenticated path

- **WHEN** the operator runs `opsx-plan approve` in a registered supervised
  job and the request reaches the broker through the operator
  OS-authenticated path
- **THEN** the broker records a durable approval receipt bound to the exact
  checkpoint and material revision, and the command reports the affected
  changes

#### Scenario: Diagnostics work during mediation

- **WHEN** any principal runs `opsx-plan status` or `opsx-plan logs` in a
  registered supervised job
- **THEN** the read-only output is produced without requiring broker
  mediation

#### Scenario: Tampered supervised markers do not disable mediation

- **WHEN** a worker-domain process removes supervised fields from the JSON
  execution state or repo plan of a registered job and then runs
  `opsx-plan approve`
- **THEN** the command is still broker mediated and the worker attempt is
  refused

### Requirement: Operator documentation describes broker-mediated approval behavior

Operator-facing documentation SHALL describe broker mediation for registered
supervised jobs: which commands are mediated, the operator OS-authenticated
approval path, the delegated-approval behavior of
`pause_before_human_only = false`, the named worker-refusal errors, the
material-revision binding of receipts (including when an explicit plan or
policy revision re-arms a gate), and the unchanged behavior of unregistered
legacy jobs.

#### Scenario: Documentation covers the mediated surface

- **WHEN** the operator workflow documentation is reviewed against this
  change
- **THEN** every element listed above is documented, with at least one
  example of an operator approval and of a worker refusal
