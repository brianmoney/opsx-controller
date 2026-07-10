# plan-operator-cli Specification

## Purpose
TBD - created by archiving change add-active-plan-resolution. Update Purpose after archive.
## Requirements
### Requirement: Operators can activate a plan for subsequent commands

The orchestrator SHALL provide an `opsx-plan use <plan.toml>` command that records the active plan for the current repository.

The active-plan record SHALL be stored under `.opsx-plan/` and SHALL contain the selected plan as a repository-relative TOML path.

The `use` command SHALL validate that the target plan exists and can be loaded before writing the active-plan record.

#### Scenario: Operator activates a plan

- **WHEN** an operator runs `opsx-plan use openspec/plans/operator-workflow-upgrades-plan.toml`
- **THEN** the command records `openspec/plans/operator-workflow-upgrades-plan.toml` as the active plan under `.opsx-plan/` and reports the activated path

#### Scenario: Invalid plan is not activated

- **WHEN** an operator runs `opsx-plan use missing.toml` or points at an invalid plan manifest
- **THEN** the command exits with a clear error and does not replace the existing active-plan record

### Requirement: Operator commands resolve an omitted plan deterministically

The `run`, `status`, `approve`, `accept`, `reset`, `report`, and `dashboard` subcommands SHALL accept an omitted plan positional argument.

When a command needs a plan path, the orchestrator SHALL resolve the plan in this precedence order:

1. An explicit command-line plan argument.
2. The `OPSX_PLAN` environment variable.
3. The active-plan pointer file under `.opsx-plan/`.

If no plan can be resolved, the command SHALL exit with an actionable error that names `opsx-plan use <plan.toml>`.

#### Scenario: Explicit plan argument wins

- **WHEN** the active-plan pointer contains `openspec/plans/active.toml`, `OPSX_PLAN` is set to `openspec/plans/env.toml`, and the operator runs `opsx-plan status openspec/plans/explicit.toml`
- **THEN** `opsx-plan` loads `openspec/plans/explicit.toml`

#### Scenario: Environment variable wins over pointer

- **WHEN** the active-plan pointer contains `openspec/plans/active.toml`, `OPSX_PLAN` is set to `openspec/plans/env.toml`, and the operator runs `opsx-plan status` with no plan argument
- **THEN** `opsx-plan` loads `openspec/plans/env.toml`

#### Scenario: Pointer is used when no higher-precedence source exists

- **WHEN** the active-plan pointer contains `openspec/plans/active.toml`, `OPSX_PLAN` is unset, and the operator runs `opsx-plan run` with no plan argument
- **THEN** `opsx-plan` loads `openspec/plans/active.toml`

#### Scenario: Missing plan source reports activation command

- **WHEN** no explicit plan argument, `OPSX_PLAN`, or active-plan pointer is available
- **THEN** the command exits before loading a plan and tells the operator to run `opsx-plan use <plan.toml>`

### Requirement: Stale active-plan pointers fail closed

If the active-plan pointer exists but references a missing file, the orchestrator SHALL fail closed with an error that includes the recorded path.

The orchestrator SHALL NOT auto-discover another plan TOML, select a nearby plan, or silently clear the stale pointer.

#### Scenario: Pointer target is missing

- **WHEN** the active-plan pointer contains `openspec/plans/deleted.toml` and that file no longer exists
- **THEN** `opsx-plan status` exits with an error naming `openspec/plans/deleted.toml` and does not load any other plan

### Requirement: Successful compile and explicit run activate plans

After a successful `opsx-plan compile <source.md> -o <plan.toml>`, the orchestrator SHALL record the output TOML as the active plan.

After `opsx-plan run <plan.toml>` is invoked with an explicit plan argument and that plan loads successfully, the orchestrator SHALL record that explicit plan as the active plan.

Failed compile or run invocations SHALL NOT update the active-plan pointer.

#### Scenario: Compile output becomes active

- **WHEN** `opsx-plan compile openspec/plans/example.md -o openspec/plans/example.toml` succeeds
- **THEN** `openspec/plans/example.toml` is recorded as the active plan

#### Scenario: Explicit run path becomes active

- **WHEN** an operator runs `opsx-plan run openspec/plans/example.toml` and the plan loads successfully
- **THEN** `openspec/plans/example.toml` is recorded as the active plan

#### Scenario: Failed compile does not replace active plan

- **WHEN** an existing active plan is recorded and `opsx-plan compile` fails validation before writing its output
- **THEN** the existing active-plan record remains unchanged

### Requirement: Status output identifies the active plan

The `opsx-plan status` command SHALL include the currently active plan path in its human-readable output when an active-plan pointer is present.

If `status` is operating on a higher-precedence explicit or `OPSX_PLAN` path that differs from the pointer, the output SHALL still identify the recorded active-plan pointer separately from the plan being inspected.

#### Scenario: Status displays active plan

- **WHEN** the active-plan pointer contains `openspec/plans/active.toml` and the operator runs `opsx-plan status`
- **THEN** the status output includes `openspec/plans/active.toml` as the active plan

#### Scenario: Status distinguishes explicit plan from active pointer

- **WHEN** the active-plan pointer contains `openspec/plans/active.toml` and the operator runs `opsx-plan status openspec/plans/other.toml`
- **THEN** the status output identifies `openspec/plans/other.toml` as the inspected plan and `openspec/plans/active.toml` as the active plan pointer

### Requirement: `opsx-plan run` supports a spend budget based on run telemetry

The orchestrator SHALL accept a `--budget-usd <amount>` flag on `opsx-plan run`.

When the flag is set to a positive value, the orchestrator SHALL accumulate estimated stage costs from telemetry records generated by the current run and SHALL stop dispatching new stages once the cumulative estimated cost reaches or exceeds the configured USD cap.

The orchestrator SHALL evaluate the spend cap only between stage dispatches. It SHALL NOT interrupt, kill, or otherwise abort a stage that is already in flight only because the spend cap would be reached or exceeded by that stage's completion.

When `--budget-usd` is omitted, the orchestrator SHALL behave exactly as it does today with respect to spend-based stopping.

#### Scenario: Run stops after cumulative estimated spend reaches the cap

- **GIVEN** `opsx-plan run --budget-usd 1.00` is driving a plan and the current run's completed stage telemetry has accumulated estimated costs of `$0.40` and `$0.35`
- **WHEN** the next completed stage for that run records an estimated cost of `$0.30`
- **THEN** the orchestrator allows that stage to finish, records its telemetry, and stops before dispatching any further stage because the cumulative estimated spend is now `$1.05`

#### Scenario: Run continues while cumulative estimated spend remains below the cap

- **GIVEN** `opsx-plan run --budget-usd 2.00` is driving a plan and the current run's completed stage telemetry has accumulated `$0.75` of estimated cost
- **WHEN** the next stage completes with an estimated cost of `$0.20`
- **THEN** the orchestrator may continue dispatching later ready stages because the cumulative estimated spend remains below the configured cap

#### Scenario: Spend cap does not affect runs without the flag

- **WHEN** an operator runs `opsx-plan run` without `--budget-usd`
- **THEN** the orchestrator does not stop stages based on cumulative estimated spend

### Requirement: Spend-budget stops report unresolved stage costs conservatively

When `opsx-plan run` stops because `--budget-usd` has been reached or exceeded, the orchestrator SHALL report:

- the cumulative estimated spend known from resolved stage costs in the current run
- the number of stages whose estimated cost contributed to that cumulative spend
- the number of stages in the current run whose cost remained unresolved

Stages with unresolved cost SHALL NOT be silently counted as zero cost.

The orchestrator MAY exclude unresolved-cost stages from the numeric cumulative spend total, but it SHALL surface their count so operators can interpret the stop conservatively.

#### Scenario: Budget stop reports mixed estimated and unresolved stage costs

- **GIVEN** `opsx-plan run --budget-usd 0.50` has completed three stages in the current run
- **AND** two stages have estimated costs of `$0.20` and `$0.35`
- **AND** one stage completed with `cost.status = "unresolved"`
- **WHEN** the orchestrator stops after the cumulative estimated spend reaches `$0.55`
- **THEN** the stop output reports `$0.55` as known cumulative estimated spend, `2` stages with resolved estimated cost, and `1` stage with unresolved cost

### Requirement: Spend-budget stops preserve resumable plan state

If `opsx-plan run` stops because `--budget-usd` has been reached or exceeded, the orchestrator SHALL preserve durable plan state so that a later run can resume using the same evidence and control-loop semantics as any other clean budget-triggered stop.

#### Scenario: Spend-budget stop leaves the run resumable

- **WHEN** `opsx-plan run --budget-usd 0.25` stops between stage dispatches after the spend cap is reached
- **THEN** the persisted `.opsx-plan/<plan-name>.state.json` remains usable for a later `opsx-plan run` resume and no in-flight stage is left half-dispatched

