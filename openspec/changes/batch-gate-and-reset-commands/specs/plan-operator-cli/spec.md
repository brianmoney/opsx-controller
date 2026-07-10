## ADDED Requirements

### Requirement: `opsx-plan` supports batch approval and acceptance gates

The orchestrator SHALL accept `opsx-plan approve --all` and `opsx-plan accept --all` for a resolved plan.

`approve --all` SHALL affect only changes currently awaiting approval.

`accept --all` SHALL affect only changes currently awaiting acceptance.

Each batch command SHALL print the exact change IDs it affected. If no changes match the requested gate state, the command SHALL report that nothing was changed.

Existing single-change `approve <change-id>` and `accept <change-id>` forms SHALL remain supported and unchanged.

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

### Requirement: `opsx-plan reset --failed` resets all failed changes to pending

The orchestrator SHALL accept `opsx-plan reset --failed` for a resolved plan.

`reset --failed` SHALL affect only changes currently in a failed state and SHALL reset each affected change to pending.

The command SHALL print the exact change IDs it reset. If no changes are failed, it SHALL report that nothing was reset.

Existing single-change `reset <change-id>` SHALL remain supported and unchanged.

#### Scenario: `reset --failed` resets every failed change

- **GIVEN** a resolved plan where `change-a` and `change-b` are failed, `change-c` is awaiting approval, and `change-d` is done
- **WHEN** the operator runs `opsx-plan reset --failed`
- **THEN** `change-a` and `change-b` are reset to pending
- **AND** the command output lists exactly `change-a` and `change-b` as reset
- **AND** `change-c` and `change-d` are left unchanged

#### Scenario: Failed reset reports an empty matching set

- **GIVEN** a resolved plan where no changes are failed
- **WHEN** the operator runs `opsx-plan reset --failed`
- **THEN** no plan state changes occur
- **AND** the command output clearly reports that no failed changes were reset

### Requirement: `opsx-plan status` prints the next unblocking command for blocked changes

When `opsx-plan status` reports a change blocked on approval, acceptance, or failure recovery, the output SHALL include the exact next command an operator can run to unblock that change.

When the inspected plan was resolved through the active-plan flow and the next command supports omitting an explicit plan argument, the status guidance SHALL use the active-plan short form rather than repeating the plan path.

The next-command guidance SHALL be specific to the blocking state:

- a change awaiting approval maps to `opsx-plan approve <change-id>`
- a change awaiting acceptance maps to `opsx-plan accept <change-id>`
- a failed change maps to `opsx-plan reset <change-id>`

#### Scenario: Status prints short-form next steps for blocked changes

- **GIVEN** the active plan is already recorded for the repository
- **AND** `change-a` is awaiting approval, `change-b` is awaiting acceptance, and `change-c` is failed
- **WHEN** the operator runs `opsx-plan status`
- **THEN** the status output includes `opsx-plan approve change-a` for `change-a`
- **AND** the status output includes `opsx-plan accept change-b` for `change-b`
- **AND** the status output includes `opsx-plan reset change-c` for `change-c`

#### Scenario: Status guidance uses the inspected plan path when short form is unavailable

- **GIVEN** the operator inspects a plan through an explicit path that differs from any recorded active-plan pointer
- **AND** `change-a` is awaiting approval in that inspected plan
- **WHEN** the operator runs `opsx-plan status openspec/plans/example.toml`
- **THEN** the status output identifies the inspected plan
- **AND** the next-step guidance includes the exact command `opsx-plan approve openspec/plans/example.toml change-a`
