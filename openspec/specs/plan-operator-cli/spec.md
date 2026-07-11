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

### Requirement: `opsx-plan logs` surfaces the latest relevant stage log for a resolved plan

The orchestrator SHALL provide an `opsx-plan logs` command for a resolved plan.

When invoked without a change or stage filter, the command SHALL select the most recent relevant stage log for that plan, print the selected log path, and print a tail of the log by default.

The orchestrator SHALL resolve the default target log from recorded plan state metadata first. If recorded state does not identify a usable log, the orchestrator SHALL fall back to deterministic `.opsx-plan/logs/` ordering.

#### Scenario: Default logs command uses recorded latest stage log metadata

- **GIVEN** the resolved plan state records `change-b` review round 2 as the latest usable stage log for that plan
- **WHEN** the operator runs `opsx-plan logs`
- **THEN** the command prints that log path
- **AND** the command prints a tail of that log

#### Scenario: Default logs command falls back to log-directory ordering

- **GIVEN** the resolved plan state does not identify a usable latest stage log
- **AND** `.opsx-plan/logs/` contains matching stage logs for the plan
- **WHEN** the operator runs `opsx-plan logs`
- **THEN** the command selects the most recent matching log by deterministic log-directory ordering
- **AND** the command prints the selected log path and a tail of that log

### Requirement: `opsx-plan logs` supports deterministic filtering and listing

The `opsx-plan logs` command SHALL support selecting logs by change id and stage for the resolved plan.

The command SHALL also support a listing mode that enumerates the available matching logs instead of tailing one selected log.

When change-id or stage filters are provided, the orchestrator SHALL apply those filters consistently whether the selected log comes from recorded state metadata or directory fallback.

#### Scenario: Operator selects a specific change and stage

- **GIVEN** the resolved plan has available logs for `change-a` implement and review and for `change-b` review
- **WHEN** the operator runs `opsx-plan logs --change change-a --stage review`
- **THEN** the command selects the latest matching `change-a` review log deterministically
- **AND** the command prints that log path and a tail of that log

#### Scenario: Operator lists available matching logs

- **GIVEN** the resolved plan has multiple available stage logs
- **WHEN** the operator runs `opsx-plan logs --list`
- **THEN** the command prints the available matching log paths for that plan instead of tailing one log

### Requirement: `opsx-plan logs` supports follow mode and clear missing-log handling

The `opsx-plan logs` command SHALL support a follow mode for an in-progress run using the same deterministic log selection rules as non-follow mode.

If no usable log matches the requested plan and filters, the command SHALL exit with a clear message rather than printing an empty tail.

#### Scenario: Follow mode tails the selected in-progress log

- **GIVEN** a resolved plan currently has an in-progress `change-c` implement log selected by the command's normal log-selection rules
- **WHEN** the operator runs `opsx-plan logs --follow`
- **THEN** the command follows that selected log instead of printing only a static tail

#### Scenario: Missing matching log reports a clear error

- **GIVEN** no usable log matches the resolved plan and requested filters
- **WHEN** the operator runs `opsx-plan logs --change missing-change --stage archive`
- **THEN** the command exits with a clear message that no matching log was found
- **AND** the command does not print an empty tail

### Requirement: Operators can run a deterministic `opsx-plan doctor` preflight

The orchestrator SHALL provide an `opsx-plan doctor [plan]` command that reports known local-environment failure modes before a run starts.

The `doctor` command SHALL emit one human-readable pass/fail line per check.

Every failing `doctor` check SHALL include a remediation hint.

If any `doctor` check fails, the command SHALL exit non-zero.

#### Scenario: Doctor reports pass/fail lines and exits non-zero on failure

- **WHEN** an operator runs `opsx-plan doctor` and at least one preflight check fails
- **THEN** the command prints a distinct pass/fail line for each check, includes a remediation hint for each failing check, and exits non-zero

### Requirement: `doctor` checks the known plan-independent environment gotchas

The `doctor` command SHALL check whether the installed orchestrator copy under `~/.local/bin` matches the repository orchestrator copy by content hash.

The `doctor` command SHALL check that required `OPSX_*_MODEL` environment variables are set.

The `doctor` command SHALL check that `openspec` and the configured adapter client executable are available on `PATH`.

The `doctor` command SHALL check that the tracked worktree contains no tracked `__pycache__` directories or tracked `.pyc` files.

The `doctor` command SHALL check that the tracked tree is clean.

#### Scenario: Doctor detects a stale installed orchestrator copy

- **WHEN** the repository copy of `opsx-plan` differs from the installed `~/.local/bin` copy
- **THEN** `opsx-plan doctor` reports that the install is stale and tells the operator to rerun the relevant installer

#### Scenario: Doctor detects missing model environment variables

- **WHEN** one or more required `OPSX_*_MODEL` environment variables are unset
- **THEN** `opsx-plan doctor` reports each missing variable and exits non-zero

#### Scenario: Doctor detects missing CLI dependencies

- **WHEN** `openspec` or the configured adapter client is not available on `PATH`
- **THEN** `opsx-plan doctor` reports the missing executable name and exits non-zero

#### Scenario: Doctor detects tracked bytecode artifacts

- **WHEN** the tracked tree contains a tracked `__pycache__` directory or tracked `.pyc` file
- **THEN** `opsx-plan doctor` reports the tracked bytecode artifact and tells the operator to remove it from version control

#### Scenario: Doctor detects a dirty tracked tree

- **WHEN** tracked files have uncommitted modifications
- **THEN** `opsx-plan doctor` reports that the tracked tree is dirty and tells the operator to clean or commit the changes before running unattended work

### Requirement: `doctor` resolves and validates plan-aware requirements when a plan is available

The `doctor` command SHALL accept an optional explicit plan argument.

When no explicit plan argument is supplied, `doctor` SHALL resolve plan identity using the same precedence order as other operator-facing commands: explicit argument, `OPSX_PLAN`, then the active-plan pointer.

If no plan can be resolved, `doctor` SHALL still run the plan-independent checks and SHALL NOT fail only because no active plan is set.

When a plan is provided or resolved, `doctor` SHALL validate that the plan loads successfully.

When the resolved plan enables pull-request delivery, `doctor` SHALL additionally require `gh` on `PATH` and at least one configured git remote.

#### Scenario: Doctor runs without a plan and checks only plan-independent prerequisites

- **WHEN** an operator runs `opsx-plan doctor` with no explicit plan, `OPSX_PLAN`, or active-plan pointer
- **THEN** the command still runs the plan-independent checks and does not fail solely because no plan was resolved

#### Scenario: Doctor uses the active plan when present

- **WHEN** the active-plan pointer contains `openspec/plans/operator-workflow-upgrades-plan.toml` and the operator runs `opsx-plan doctor`
- **THEN** the command validates `openspec/plans/operator-workflow-upgrades-plan.toml` and applies any plan-conditional checks for that plan

#### Scenario: Doctor fails on an invalid resolved plan

- **WHEN** `opsx-plan doctor openspec/plans/broken.toml` is run and the plan cannot be loaded
- **THEN** the command reports the plan load failure and exits non-zero

#### Scenario: Doctor checks pull-request delivery prerequisites for a plan that enables PR creation

- **WHEN** the resolved plan enables pull-request delivery
- **AND** `gh` is missing from `PATH` or no git remote is configured
- **THEN** `opsx-plan doctor` reports the missing delivery prerequisite and exits non-zero before any run is started

### Requirement: Run start reuses doctor checks as warnings only

At `opsx-plan run` start, the orchestrator SHALL execute the same preflight checks covered by `doctor`.

Failures detected during `run` startup SHALL be reported as warnings only and SHALL NOT change run dispatch, run exit criteria, or any other run outcome.

#### Scenario: Run start surfaces warnings without blocking dispatch

- **WHEN** a preflight check that would fail under `opsx-plan doctor` is present at `opsx-plan run` start
- **THEN** `opsx-plan run` prints the issue as a warning and continues using its normal run-control semantics

### Requirement: Plans may configure a best-effort run-event notification command

The orchestrator SHALL support an optional `plan.notify_cmd` setting for a resolved plan.

When `plan.notify_cmd` is configured, the orchestrator SHALL invoke that command with exactly one argument containing a JSON-encoded notification event payload.

When `plan.notify_cmd` is absent, `opsx-plan` SHALL behave exactly as it does today and SHALL emit no notification-command side effects.

#### Scenario: Plan without `notify_cmd` runs unchanged

- **GIVEN** a resolved plan that does not set `plan.notify_cmd`
- **WHEN** the operator runs `opsx-plan run`
- **THEN** the orchestrator emits no notification command
- **AND** run behavior is otherwise unchanged

### Requirement: Notification payloads use a stable event schema

Each notification payload SHALL be a JSON object containing:

- `event_type`: a string naming the event
- `plan_name`: the resolved plan name
- `timestamp`: the orchestrator-generated event timestamp
- `summary`: a short human-readable description of the event

For change-specific events, the payload SHALL also include `change_id`.

For plan-wide events that do not apply to a single change, the payload SHALL omit `change_id` rather than inventing one.

#### Scenario: Change-specific event includes `change_id`

- **GIVEN** a resolved plan emits a notification because `change-a` reaches a listed change-specific transition
- **WHEN** the orchestrator invokes `plan.notify_cmd`
- **THEN** the JSON payload includes `event_type`, `plan_name`, `timestamp`, `summary`, and `change_id = "change-a"`

#### Scenario: Plan-wide event omits `change_id`

- **GIVEN** a resolved plan emits a notification because the whole plan completes
- **WHEN** the orchestrator invokes `plan.notify_cmd`
- **THEN** the JSON payload includes `event_type`, `plan_name`, `timestamp`, and `summary`
- **AND** the payload does not include `change_id`

### Requirement: The orchestrator emits notifications for listed change and delivery milestones

When `plan.notify_cmd` is configured, the orchestrator SHALL emit exactly one notification for each of these run events when they occur:

- a change becomes done
- a change becomes failed
- a change becomes awaiting approval
- a change becomes awaiting acceptance
- the whole plan completes
- pull-request delivery opens a pull request

The pull-request-opened notification SHALL only be emitted after pull-request creation succeeds and the resulting URL is authoritative in plan state.

#### Scenario: Awaiting-approval transition emits one notification

- **GIVEN** `plan.notify_cmd` is configured
- **AND** `change-a` reaches the awaiting-approval state during a run
- **WHEN** that transition is persisted
- **THEN** the orchestrator invokes the notification command exactly once for that awaiting-approval event

#### Scenario: Pull-request-opened event follows successful PR delivery

- **GIVEN** `plan.notify_cmd` is configured
- **AND** a completed plan successfully opens its configured pull request
- **WHEN** the orchestrator records the authoritative pull-request result
- **THEN** it invokes the notification command exactly once for the pull-request-opened event

### Requirement: Notification-command failures never change run outcomes

If invoking `plan.notify_cmd` fails, exits non-zero, or crashes, the orchestrator SHALL log the notification failure for operator triage.

The orchestrator SHALL NOT treat notification-command failure as a stage failure, SHALL NOT roll back or suppress the underlying plan-state transition, and SHALL NOT change whether the overall run succeeds, pauses, or fails for its real execution reason.

#### Scenario: Crashing notification hook does not fail the underlying transition

- **GIVEN** `plan.notify_cmd` is configured
- **AND** `change-a` becomes done
- **AND** the notification command exits non-zero for that event
- **WHEN** the orchestrator handles the transition
- **THEN** `change-a` remains recorded as done
- **AND** the notification failure is logged
- **AND** the run outcome is determined by the underlying plan execution rather than the hook failure

### Requirement: Operator documentation describes the upgraded `opsx-plan` workflow end to end

The repository SHALL provide operator-facing documentation for `opsx-plan` that
explains the upgraded workflow from plan compilation and activation through run
supervision.

That documentation SHALL cover, at minimum:

- active-plan activation and omitted-plan resolution precedence
- `opsx-plan doctor` preflight usage
- `opsx-plan run` with time and spend budget controls
- batch `approve --all`, `accept --all`, and `reset --failed` gate handling
- `opsx-plan logs` usage for current or recent stage output
- notification hook behavior and event coverage

The documentation SHALL include at least one worked example that starts with
`opsx-plan compile` and continues through an operator-driven plan run using the
final command surface.

#### Scenario: Operator docs cover the final CLI workflow

- **WHEN** an operator reads the documented `opsx-plan` workflow after the
  operator-workflow-upgrade series lands
- **THEN** the documentation includes activation, doctor, run, budgets, gate
  controls, logs, notifications, and a worked compile-to-run example using the
  final command names

### Requirement: Operator documentation makes default-off and override behavior explicit

The same operator documentation SHALL identify which new operator-facing
features are disabled by default and SHALL document the one-run overrides or
precedence rules that change behavior for a single invocation.

At minimum, the documentation SHALL explicitly describe:

- the precedence of explicit plan argument, `OPSX_PLAN`, and the active-plan pointer
- that budget controls are opt-in flags
- the operator-visible outcome of doctor failures and budget-triggered stops

#### Scenario: Operator docs explain defaults and precedence clearly

- **WHEN** an operator checks whether a new CLI workflow feature is always on,
  optional, or invocation-scoped
- **THEN** the documentation states the default behavior and names the
  precedence rule or flag that changes it
