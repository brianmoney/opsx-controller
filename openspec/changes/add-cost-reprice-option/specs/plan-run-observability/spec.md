## ADDED Requirements

### Requirement: Report command supports read-only cost reprice

The `opsx-plan report` command SHALL accept an optional `--reprice` flag. When
the flag is set, the command SHALL recompute each selected telemetry record's
`cost` object in memory from that record's stored `usage` and `model` fields
against the currently loaded pricing catalog, before aggregation, using the
same estimation routine that stage dispatch uses. The command SHALL NOT write
to the telemetry JSONL, the plan state file, or any other repository file.

When `--reprice` is set, report output SHALL state that costs were repriced and
SHALL name the pricing catalog version used for the recomputation, so a
repriced figure is distinguishable from the stored figure. When the flag is
absent, the report SHALL use the stored `cost` values unchanged.

#### Scenario: Reprice resolves previously unresolved costs
- GIVEN telemetry in which a stage record has `cost.status = "unresolved"`
  because its model had no catalog entry at write time
- AND the current pricing catalog now contains an entry for that model
- WHEN the operator runs `opsx-plan report --reprice`
- THEN the recomputed record has `cost.status = "estimated"` with a non-null
  `estimated_cost`
- AND the plan total estimated cost includes the recomputed value

#### Scenario: Reprice does not mutate telemetry
- WHEN an operator runs `opsx-plan report --reprice` against a completed plan
- THEN `.opsx-plan/telemetry/<plan_name>.jsonl` and
  `.opsx-plan/<plan_name>.state.json` are byte-for-byte unchanged

#### Scenario: Still-unpriced records remain explicit
- GIVEN a stage record whose model still has no catalog entry after reprice
- WHEN the operator runs `opsx-plan report --reprice`
- THEN that record keeps `cost.status = "unresolved"` with a stable
  `unresolved_reason`
- AND no cost is fabricated for it

#### Scenario: Default report is unchanged
- WHEN the operator runs `opsx-plan report` without `--reprice`
- THEN the output is identical to the pre-change behaviour that reads stored
  `cost` values

#### Scenario: Repriced report is deterministic
- WHEN `opsx-plan report --reprice` is run twice against the same telemetry and
  the same pricing catalog
- THEN both runs produce identical output

### Requirement: Dashboard command supports read-only cost reprice

The `opsx-plan dashboard` command SHALL accept an optional `--reprice` flag
with the same recomputation and non-mutation semantics as the report command.
When the flag is set, the generated HTML SHALL state that costs were repriced
and SHALL name the pricing catalog version used.

#### Scenario: Repriced dashboard names the catalog version
- WHEN the operator runs `opsx-plan dashboard --reprice`
- THEN the generated HTML indicates that costs were repriced and identifies the
  catalog version
- AND telemetry and state files are unchanged

#### Scenario: Default dashboard is unchanged
- WHEN the operator runs `opsx-plan dashboard` without `--reprice`
- THEN the generated HTML is unchanged from the pre-change behaviour
