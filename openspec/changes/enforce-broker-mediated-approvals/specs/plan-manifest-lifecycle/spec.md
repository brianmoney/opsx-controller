## MODIFIED Requirements

### Requirement: Supervision storage leaves JSON execution state authoritative and unchanged

For unregistered legacy jobs, the JSON execution state under `.opsx-plan/`
SHALL remain the authoritative state record for plan execution, whether or
not any supervisor ledger exists. The supervisor ledger SHALL NOT be stored
under `.opsx-plan/` or anywhere else inside the repository worktree, and
introducing supervision storage SHALL NOT change the JSON execution state's
format, location, or read/write semantics for unregistered jobs. Legacy jobs
without a supervised registration SHALL keep their existing JSON handling
with no dependency on the supervisor package or ledger.

For a registered supervised job, the broker and supervisor ledger SHALL be
the phase authority, and the JSON execution state SHALL be a projection of
broker and ledger state rather than a competing authority: direct JSON writes
SHALL NOT release gates, satisfy checkpoints, or alter supervised identity,
and the projection SHALL be derivable from broker-recorded receipts and
ledger records.

#### Scenario: Legacy runs are untouched by the supervisor package

- **WHEN** an ordinary (non-supervised) plan run executes with the supervisor
  package present
- **THEN** its JSON execution state handling is unchanged and no supervisor
  ledger is created or consulted

#### Scenario: The ledger never lands in the worktree

- **WHEN** a supervisor ledger is created for a supervised job
- **THEN** the ledger file resides in service-owned storage outside the
  repository worktree, and no ledger file appears under `.opsx-plan/`

#### Scenario: The JSON projection follows broker state

- **WHEN** a broker receipt changes the gate state of a registered job and
  the JSON projection is regenerated
- **THEN** the projected state matches the broker and ledger records, and any
  direct JSON edit that disagrees with them has no authority
