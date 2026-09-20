# plan-operator-cli Specification

## Purpose
TBD - created by archiving change add-active-plan-resolution. Update Purpose after archive.

## Requirements

### Requirement: Operators can archive a completed plan

The orchestrator SHALL provide `opsx-plan archive-plan <plan.toml>` that retires a completed authored plan by moving its compiled manifest, and its markdown source when present, into `openspec/plans/archived/`.

The command SHALL move tracked files with `git mv` and untracked files with a plain rename, determining tracked status before moving. The command SHALL NOT create a commit, and SHALL report the moved paths and that the move needs committing.

The command SHALL fail without moving anything when the target is already under `openspec/plans/archived/`, when the target is not under `openspec/plans/`, or when the target does not exist.

When the active-plan pointer references the plan being archived, the command SHALL clear the pointer and SHALL report that it did so. The command SHALL NOT repoint the pointer at the archived copy, because operating on an archived plan mutates its state destructively.

#### Scenario: Plan pair is archived
- **WHEN** an operator runs `opsx-plan archive-plan openspec/plans/example.toml` and `openspec/plans/example.md` exists
- **THEN** both files are moved into `openspec/plans/archived/`, the moved paths are reported, and no commit is created

#### Scenario: Manifest without a markdown source is archived
- **WHEN** an operator runs `opsx-plan archive-plan openspec/plans/example.toml` and no `openspec/plans/example.md` exists
- **THEN** the manifest is moved into `openspec/plans/archived/` and the command succeeds without reporting a missing markdown source as an error

#### Scenario: Archiving the active plan clears the pointer
- **WHEN** the active-plan pointer contains `openspec/plans/example.toml` and the operator runs `opsx-plan archive-plan openspec/plans/example.toml`
- **THEN** the active-plan pointer is cleared, the command reports that it was cleared, and the pointer is not set to the archived path

#### Scenario: Archiving a different plan leaves the pointer intact
- **WHEN** the active-plan pointer contains `openspec/plans/active.toml` and the operator runs `opsx-plan archive-plan openspec/plans/other.toml`
- **THEN** `openspec/plans/other.toml` is archived and the active-plan pointer still contains `openspec/plans/active.toml`

#### Scenario: Double archive is refused
- **WHEN** an operator runs `opsx-plan archive-plan openspec/plans/archived/example.toml`
- **THEN** the command exits with an error and no file is moved

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

When resolving a plan for a command, the orchestrator SHALL NOT auto-discover another plan TOML, select a nearby plan, or silently clear the stale pointer.

This constraint governs plan resolution. It SHALL NOT prevent a command whose explicit purpose is to retire a plan from clearing the pointer as a reported part of that operation, before the pointer becomes stale.

#### Scenario: Pointer target is missing

- **WHEN** the active-plan pointer contains `openspec/plans/deleted.toml` and that file no longer exists
- **THEN** `opsx-plan status` exits with an error naming `openspec/plans/deleted.toml` and does not load any other plan

#### Scenario: Resolution never self-heals a stale pointer

- **WHEN** the active-plan pointer references a missing file and other plan TOML files exist under `openspec/plans/`
- **THEN** the orchestrator still fails closed and leaves the pointer unchanged rather than selecting one of the available plans

### Requirement: Successful compile and explicit run activate plans

After a successful `opsx-plan compile <source.md> -o <plan.toml>`, the orchestrator SHALL record the output TOML as the active plan.

The `-o` argument SHALL be optional. When it is omitted, the orchestrator SHALL compile to `openspec/plans/<source-stem>.toml` and SHALL record that defaulted output as the active plan on success, identically to an explicitly specified output.

After `opsx-plan run <plan.toml>` is invoked with an explicit plan argument and that plan loads successfully, the orchestrator SHALL record that explicit plan as the active plan.

Failed compile or run invocations SHALL NOT update the active-plan pointer.

Single-change runs SHALL NOT update the active-plan pointer, whether they succeed or fail.

#### Scenario: Compile output becomes active

- **WHEN** `opsx-plan compile openspec/plans/example.md -o openspec/plans/example.toml` succeeds
- **THEN** `openspec/plans/example.toml` is recorded as the active plan

#### Scenario: Defaulted compile output becomes active

- **WHEN** `opsx-plan compile openspec/plans/example.md` succeeds with no output argument
- **THEN** the manifest is written to `openspec/plans/example.toml` and that path is recorded as the active plan

#### Scenario: Explicit run path becomes active

- **WHEN** an operator runs `opsx-plan run openspec/plans/example.toml` and the plan loads successfully
- **THEN** `openspec/plans/example.toml` is recorded as the active plan

#### Scenario: Failed compile does not replace active plan

- **WHEN** an existing active plan is recorded and `opsx-plan compile` fails validation before writing its output
- **THEN** the existing active-plan record remains unchanged

#### Scenario: Single-change run does not replace active plan

- **WHEN** the active-plan pointer contains `openspec/plans/active.toml` and an operator runs `opsx-run vault-gardening-suggestions` to completion
- **THEN** the active-plan pointer still contains `openspec/plans/active.toml` and does not reference the derived single-change manifest

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

### Requirement: `opsx-plan models` exposes model resolution to operators

The orchestrator SHALL provide an `opsx-plan models` subcommand group alongside the other operator-facing commands.

`opsx-plan models show [--adapter <name>]` SHALL print one line per role giving the role name, the resolved model, and the resolution source, and SHALL report any identifier-syntax violations for the target adapter.

`opsx-plan models env [--adapter <name>]` SHALL emit shell `export` statements for the four resolved `OPSX_*_MODEL` variables and SHALL exit non-zero if any role is unresolved.

`opsx-plan models init` SHALL create the user-global model configuration file, pre-populating role values from the current environment where set, and SHALL NOT overwrite an existing file without an explicit force flag.

When `--adapter` is omitted, `models show` and `models env` SHALL resolve the adapter from the active plan using the same plan-resolution precedence as other operator commands. When `--adapter` is supplied, they SHALL NOT require a resolvable plan.

#### Scenario: Operator inspects the active plan's models

- **WHEN** an operator runs `opsx-plan models show` with an active plan whose adapter is `claude-code`
- **THEN** the command reports the four roles resolved for `claude-code`, each with its resolution source

#### Scenario: Operator inspects an explicit adapter without an active plan

- **WHEN** an operator runs `opsx-plan models show --adapter opencode` with no active plan set
- **THEN** the command resolves and prints the `opencode` model set instead of failing for lack of a plan

#### Scenario: Models init refuses to clobber an existing file

- **WHEN** an operator runs `opsx-plan models init` and the user-global configuration file already exists
- **THEN** the command leaves the existing file unchanged and reports that a force flag is required to replace it

### Requirement: Operators can run a deterministic `opsx-plan doctor` preflight

The orchestrator SHALL provide an `opsx-plan doctor [plan] [--adapter <adapter>]` command that reports known local-environment failure modes before a run or compile starts.

The `doctor` command SHALL emit one human-readable pass/fail line per check.

Every failing `doctor` check SHALL include a remediation hint.

If any `doctor` check fails, the command SHALL exit non-zero.

#### Scenario: Doctor reports pass/fail lines and exits non-zero on failure

- **WHEN** an operator runs `opsx-plan doctor` and at least one preflight check fails
- **THEN** the command prints a distinct pass/fail line for each check, includes a remediation hint for each failing check, and exits non-zero

#### Scenario: Plan-less doctor selects a compile adapter

- **WHEN** an operator runs `opsx-plan doctor --adapter claude-code` with no resolvable plan
- **THEN** doctor preflights Claude Code model and client prerequisites

### Requirement: `doctor` checks the known plan-independent environment gotchas

The `doctor` command SHALL check whether the installed orchestrator copy under `~/.local/bin` matches the repository orchestrator copy by content hash.

The `doctor` command SHALL check that every model role resolves for the target adapter, reporting each resolved model together with its resolution source, and SHALL fail when any role is unresolved. The target adapter SHALL be the resolved plan's adapter when a plan is available; otherwise it SHALL be the explicit `--adapter` value or the default `opencode` value.

The `doctor` command SHALL check that each resolved model identifier is valid for the target adapter's identifier syntax, and SHALL fail when a resolved identifier is provider-prefixed for the `claude-code` adapter or lacks a `provider/` prefix for the `opencode` adapter.

The `doctor` command SHALL check that the configured adapter client executable is available on `PATH`, and that the `openspec` CLI is available either repo-locally (`<repo>/node_modules/.bin/openspec`) or, failing that, on `PATH`. `openspec` resolution SHALL prefer the repo-local install over the global one.

The `doctor` command SHALL check that the tracked worktree contains no tracked `__pycache__` directories or tracked `.pyc` files.

The `doctor` command SHALL check that the tracked tree is clean.

#### Scenario: Doctor detects a stale installed orchestrator copy

- **WHEN** the repository copy of `opsx-plan` differs from the installed `~/.local/bin` copy
- **THEN** `opsx-plan doctor` reports that the install is stale and tells the operator to rerun the relevant installer

#### Scenario: Doctor detects unresolved model roles

- **WHEN** one or more model roles cannot be resolved for the target adapter
- **THEN** `opsx-plan doctor` reports each unresolved role and exits non-zero

#### Scenario: Doctor reports the resolution source for each model

- **WHEN** an operator runs `opsx-plan doctor` and all roles resolve
- **THEN** the model check reports each resolved model together with whether it came from a configuration file or the ambient environment

#### Scenario: Doctor rejects a provider-prefixed identifier under Claude Code

- **WHEN** the target adapter is `claude-code` and a role resolves to a provider-prefixed identifier such as `deepseek/deepseek-v4-pro`
- **THEN** `opsx-plan doctor` fails the identifier-syntax check, names the offending role, and exits non-zero instead of allowing the run to fail later at stage dispatch

#### Scenario: Doctor rejects a bare identifier under OpenCode

- **WHEN** the target adapter is `opencode` and a role resolves to an identifier with no `provider/` prefix
- **THEN** `opsx-plan doctor` fails the identifier-syntax check, names the offending role, and exits non-zero

#### Scenario: Doctor detects missing CLI dependencies

- **WHEN** `openspec` is unavailable both repo-locally and on `PATH`, or the configured adapter client is not available on `PATH`
- **THEN** `opsx-plan doctor` reports the missing executable name, tells the operator to initialize OpenSpec (e.g. `npx openspec@latest init`), and exits non-zero

#### Scenario: Doctor prefers a repo-local openspec install

- **WHEN** the repository has `node_modules/.bin/openspec` and `openspec` is also on `PATH`
- **THEN** `opsx-plan doctor` resolves the repo-local binary for the `openspec` check

#### Scenario: Doctor accepts a repo-local openspec when global is absent

- **WHEN** the repository has `node_modules/.bin/openspec` and no `openspec` is on `PATH`
- **THEN** `opsx-plan doctor` reports the `openspec` check as passing

#### Scenario: Doctor detects tracked bytecode artifacts

- **WHEN** the tracked tree contains a tracked `__pycache__` directory or tracked `.pyc` file
- **THEN** `opsx-plan doctor` reports the tracked bytecode artifact and tells the operator to remove it from version control

#### Scenario: Doctor detects a dirty tracked tree

- **WHEN** tracked files have uncommitted modifications
- **THEN** `opsx-plan doctor` reports that the tracked tree is dirty and tells the operator to clean or commit the changes before running unattended work

### Requirement: `doctor` verifies worker agents for direct-dispatch plans

When the resolved plan uses direct dispatch, `doctor` SHALL check that the configured adapter's implement, review, and archive worker agents are installed in that adapter's agent directory.

The check SHALL report each missing worker agent by name, SHALL name the installer that provides it, and SHALL exit non-zero.

When no plan is resolved, or when the resolved plan does not use direct dispatch, the check SHALL be skipped without failing the command.

#### Scenario: Doctor detects a missing worker agent

- **WHEN** the resolved plan uses direct dispatch and one of the adapter's worker agents is not installed
- **THEN** `opsx-plan doctor` reports the missing agent by name, points at the adapter installer, and exits non-zero

#### Scenario: Doctor passes when all worker agents are installed

- **WHEN** the resolved plan uses direct dispatch and all three worker agents are present in the adapter's agent directory
- **THEN** the worker-agent check passes and does not affect the command outcome

#### Scenario: Doctor skips the check for a nested-controller plan

- **WHEN** the resolved plan does not configure a full set of stage invokes
- **THEN** `opsx-plan doctor` skips the worker-agent check and does not fail because of it

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

### Requirement: Operator documentation covers the `pause_before_human_only` key

Operator-facing documentation for `opsx-plan` manifests SHALL describe the
`pause_before_human_only` key: its human-only default when absent on a gated
change, the explicit `false` delegation opt-out, and the invalidity of
`true` without `pause_before = true`.

#### Scenario: Workflow documentation explains the flag

- **WHEN** an operator reads the manifest key documentation and the
  manual-gates section of the operator workflow documentation
- **THEN** the key, its human-only default, the delegation opt-out, and the
  invalid combination are all described

### Requirement: The `opsx-plan supervise` namespace reports backend capability

The orchestrator SHALL provide an `opsx-plan supervise` command namespace.
Its capability report SHALL state whether the host provides a supported
isolation backend for the operator authority boundary, naming the detected
backend status plainly so an operator can tell whether supervision can be
enabled before attempting it.

The capability report SHALL be a read-only diagnostic: it SHALL NOT create
accounts, install service units, write the authority store, or change any
host configuration, and it SHALL run without requiring the boundary to be
active.

#### Scenario: A supported host reports the backend

- **WHEN** an operator runs the `opsx-plan supervise` capability report on a
  host with a supported isolation backend
- **THEN** the report states that the backend is available and exits
  successfully

#### Scenario: An unsupported host reports unavailability

- **WHEN** an operator runs the `opsx-plan supervise` capability report on a
  host without a supported isolation backend
- **THEN** the report states that no supported backend is available, and the
  report itself makes no change to the host

### Requirement: Enabling supervision fails closed on an unsupported host

When the capability surface is asked to enable supervision and the isolation
backend is unavailable, the command SHALL exit non-zero with a named
unsupported-host error. The command SHALL NOT silently downgrade to a weaker
isolation posture and SHALL NOT provision accounts or services automatically:
provisioning is a separate, manual operator step the error output points to.

#### Scenario: Enablement refused with a named error

- **WHEN** an operator attempts to enable supervision through `opsx-plan
  supervise` on a host without a supported backend
- **THEN** the command exits non-zero, names the unsupported-host error,
  enables nothing, and substitutes no weaker posture

#### Scenario: Enablement never auto-provisions

- **WHEN** an operator attempts to enable supervision on any host
- **THEN** the command creates no accounts, installs no service units, and
  directs the operator to the manual provisioning step instead

### Requirement: Existing diagnostics remain available without the boundary

The independent diagnostic commands `opsx-plan doctor`, `opsx-plan status`,
`opsx-plan logs`, and `opsx-plan report` SHALL remain fully available on a
host without a supported isolation backend and without any supervision
enablement, so legacy unsupervised operation keeps its observability
unchanged.

#### Scenario: Diagnostics run on an unsupported host

- **WHEN** an operator runs `doctor`, `status`, `logs`, or `report` on a host
  with no supported isolation backend and no supervised job
- **THEN** each command behaves exactly as it does for legacy unsupervised
  runs, with no boundary-related failure

### Requirement: Operator documentation describes the boundary behavior

The operator-facing `opsx-plan` documentation SHALL describe the `supervise`
namespace's capability report, the named unsupported-host error, the
fail-closed refusal with no silent downgrade, and the manual provisioning
stance.

#### Scenario: The boundary behavior is documented

- **WHEN** an operator reads the documented `opsx-plan` supervision surface
- **THEN** it shows the capability report, names the unsupported-host error,
  states that no downgrade or automatic provisioning occurs, and points to
  the manual provisioning step

### Requirement: Mutating commands acquire the worktree execution lock

The mutating commands `opsx-plan run`, `opsx-plan reset`, and the
single-change handler shared by `opsx-run` and its documented alias
`opsx-plan run-one` SHALL acquire the worktree execution lock before
performing any mutating work and SHALL hold it for the duration of the
command, releasing it on every exit path, normal or failed.

When the lock is already held, the command SHALL fail with a named
lock-contention error and a non-zero exit code rather than waiting for the
lock or proceeding without it. Future supervised mutating paths (such as
supervised recovery) SHALL acquire the same lock when they are introduced.

#### Scenario: A mutating command holds the lock for its duration

- **WHEN** an operator runs `opsx-plan run`, `opsx-plan reset`, `opsx-run`,
  or `opsx-plan run-one` and no other process holds the worktree lock
- **THEN** the command acquires the lock before mutating anything, holds it
  until it exits, and releases it on both success and failure

#### Scenario: Both names of the single-change command serialize

- **WHEN** `opsx-run` holds the worktree lock and `opsx-plan run-one` is
  invoked in the same worktree (or the reverse)
- **THEN** the second invocation exits non-zero with the named
  lock-contention error and performs no mutating work, because both names
  dispatch to the same handler and acquire the same lock

#### Scenario: Contention fails fast with a named error

- **WHEN** a mutating command is invoked while another process holds the
  worktree lock
- **THEN** it exits non-zero with a named lock-contention error, performs no
  mutating work, and leaves the holder undisturbed

### Requirement: An ordinary mutating command is refused when it would race a supervised execution

When the worktree lock is held by a supervised execution, an ordinary
mutating command (`run`, `reset`, `opsx-run`, or its alias
`opsx-plan run-one`) SHALL be refused with a documented named error stating
that the worktree is owned by a supervised execution. This refusal is the one
intentional behavior change for legacy runs; every other legacy behavior
SHALL be preserved.

#### Scenario: Ordinary run refused during supervised execution

- **WHEN** a supervised execution holds the worktree lock and an operator
  runs `opsx-plan run` or `opsx-plan reset` in that worktree
- **THEN** the command exits non-zero with the documented named
  supervised-ownership error and performs no mutating work

#### Scenario: Ordinary run proceeds after the supervised execution releases

- **WHEN** the supervised execution has released the worktree lock
- **THEN** an ordinary mutating command acquires the lock and proceeds with
  its normal legacy behavior

### Requirement: Diagnostics and gate commands do not acquire the execution lock

The read-only diagnostic commands `opsx-plan doctor`, `opsx-plan status`,
`opsx-plan logs`, `opsx-plan report`, and `opsx-plan dashboard` SHALL run
without acquiring the worktree execution lock, including while another
process holds it.

The gate commands `opsx-plan approve` and `opsx-plan accept` SHALL record
their receipts without acquiring the worktree execution lock, so an
operator can always release a gate while an execution is running or
waiting.

#### Scenario: Diagnostics run during a held lock

- **WHEN** a mutating command holds the worktree lock
- **THEN** `doctor`, `status`, `logs`, `report`, and `dashboard` still run
  to completion in that worktree

#### Scenario: A gate command succeeds during a held lock

- **WHEN** a mutating command holds the worktree lock and a change is
  awaiting approval
- **THEN** `opsx-plan approve <change-id>` records the approval without
  acquiring the lock and without failing for lock contention

### Requirement: Operator documentation describes the execution lock behavior

The operator-facing `opsx-plan` documentation SHALL describe the worktree
execution lock: which commands acquire it, the named lock-contention error,
the documented refusal of an ordinary mutating command that would race a
supervised execution, and that diagnostics and gate commands never block on
the lock.

#### Scenario: The lock behavior is documented

- **WHEN** an operator reads the documented `opsx-plan` workflow
- **THEN** it names the lock-acquiring commands, shows the contention and
  supervised-ownership errors, and states that diagnostics and approvals
  run without the lock

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

### Requirement: `opsx-plan supervise` provides the supervised job lifecycle commands

The `opsx-plan supervise` namespace SHALL provide the lifecycle commands
`register`, `start`, `inspect`, `resume`, `pause`, `drain`, and `cancel`
alongside its existing capability, probe, and serve commands.

`register` SHALL record the supervised job for the resolved plan and SHALL
fail with the named unsupported-host error on a host without a supported
isolation backend. `start`, `resume`, `pause`, `drain`, and `cancel`
targeting a worktree with no registered supervised job SHALL exit non-zero
with a named unknown-job error. A command requesting an illegal transition
SHALL exit non-zero with a named illegal-transition error, and a mutating
command targeting a terminal job SHALL exit non-zero with a named
terminal-job error.

Mutating lifecycle commands for a live job SHALL be mediated through the
operator OS-authenticated path, consistent with broker mediation: a
worker-domain process SHALL NOT be able to invoke them. When the required
authority path is unreachable, the command SHALL fail closed with the named
broker-unavailable error rather than acting unmediated.

`inspect` SHALL be a read-only projection of the job: its state, recorded
waits, policy revision, budget posture, and recent actions and incidents.
It SHALL require neither the worktree execution lock nor a live service, and
SHALL exit non-zero with a named unknown-job error when no registered job
exists.

These commands SHALL NOT affect legacy unregistered runs: an operator who
never registers a supervised job observes no behavior change in any existing
command.

#### Scenario: Register then start a supervised job

- **WHEN** an operator runs `opsx-plan supervise register` for a plan on a
  supported host and then `opsx-plan supervise start`
- **THEN** the job is recorded with its full registration fields and
  transitions to `active`

#### Scenario: Lifecycle commands fail closed with named errors

- **WHEN** a mutating lifecycle command targets an unregistered worktree, an
  illegal transition, a terminal job, an unreachable authority path, or an
  unsupported host
- **THEN** it exits non-zero naming the corresponding error — unknown-job,
  illegal-transition, terminal-job, broker-unavailable, or unsupported-host
  — and records nothing

#### Scenario: Pause, drain, resume, and cancel drive the state machine

- **WHEN** the operator runs `pause`, `drain`, `resume`, or `cancel` for a
  registered job in a legal source state
- **THEN** each command records its durable effect and drives the documented
  transition, and the effects are visible to a later `inspect`

#### Scenario: Inspect is read-only and lock-free

- **WHEN** an operator runs `inspect` for a registered job while its
  execution holds the worktree lock or no service is live
- **THEN** the command prints the job's state, waits, policy revision,
  budget posture, and recent actions and incidents without acquiring the
  lock or contacting a service

#### Scenario: A worker process cannot invoke lifecycle mutation

- **WHEN** a worker-domain process attempts to invoke a mutating lifecycle
  verb, including through the worker-actions endpoint
- **THEN** the attempt is refused and no lifecycle effect is recorded

### Requirement: Operator documentation describes the supervised lifecycle

The operator-facing `opsx-plan` documentation SHALL describe the supervised
job lifecycle: the state machine, each lifecycle command with its named
errors, the pause-versus-drain stop boundaries, cancellation effects, the
durable human wait, evidence-based completion with fresh review on
revalidation, and the `(manual)` operator checklist reporting.

#### Scenario: The lifecycle surface is documented

- **WHEN** an operator reads the documented `opsx-plan supervise` reference
- **THEN** it covers the state machine, every lifecycle command, the stop
  boundaries, cancellation, human waits, completion evidence semantics, and
  the manual-task checklist

### Requirement: `opsx-plan status` surfaces supervised job state

`opsx-plan status` SHALL surface the supervised job state for the resolved
plan when a supervised job is registered: job state and progress, open human
and stop waits, policy revision, budget posture, and recent incidents.
`opsx-plan status --json` SHALL emit the same information as a structured
document that includes a supervision object. For a plan with no registered
supervised job, `status` SHALL keep its current human output exactly and the
new structured mode SHALL omit the supervision object.

#### Scenario: Status shows a supervised job block

- **WHEN** `opsx-plan status` runs for a plan with a registered supervised job
- **THEN** it reports the job state, waits, policy revision, budget posture, and recent incidents in addition to the existing change list

#### Scenario: Status JSON emits the supervision object

- **WHEN** `opsx-plan status --json` runs for a plan with a registered supervised job
- **THEN** the document includes a supervision object with the projected job state

#### Scenario: Unregistered plans keep their status output

- **WHEN** `opsx-plan status` runs for a plan with no registered supervised job
- **THEN** its output is identical to the pre-existing behavior

### Requirement: Operator steering commands return a durable request identity and safe-boundary acknowledgement

For a registered supervised job, the `opsx-plan` operator steering commands —
a policy revision, pause-after-change, stop or retry, and cancel — SHALL
report the durable request identity of the recorded request and SHALL report
the safe-boundary acknowledgement once it is reached. A request that cannot
be recorded SHALL fail closed with a named error, and the commands SHALL NOT
change behavior for unregistered plans.

#### Scenario: A steering command returns a request identity

- **WHEN** an operator runs a steering command for a registered supervised job
- **THEN** the command reports the recorded request identity and, once reached, the safe-boundary acknowledgement

#### Scenario: Unregistered plans are unaffected

- **WHEN** a steering command is run for a plan with no registered supervised job
- **THEN** existing behavior is unchanged and no supervision request identity is reported

### Requirement: `opsx-plan supervise` provides the watchdog surface

The `opsx-plan supervise` namespace SHALL provide a `watchdog` command that
runs the deterministic watchdog loop or, with `--once`, exactly one tick. It
SHALL report each supervised job's classification and recent reconstitution
events, in human-readable form and as structured JSON. The command SHALL
require neither the worktree execution lock nor a live service, and SHALL exit
non-zero with a named unknown-job error when no registered supervised job
exists.

`opsx-plan supervise serve` SHALL run the boot-scan reconciliation and the
periodic watchdog tick as part of the service host, so the service-owned loop
supervises registered jobs unattended.

The watchdog surface SHALL NOT affect legacy unregistered runs: an operator who
never registers a supervised job observes no behavior change in any existing
command.

#### Scenario: A single tick runs from the command line

- **WHEN** an operator runs `opsx-plan supervise watchdog --once` for a
  registered job
- **THEN** exactly one tick is evaluated and the job's classification and any
  reconstitution events are reported

#### Scenario: The watchdog surface fails closed without a registered job

- **WHEN** `opsx-plan supervise watchdog` runs for a worktree with no
  registered supervised job
- **THEN** it exits non-zero with a named unknown-job error and records nothing

#### Scenario: Serve supervises unattended

- **WHEN** `opsx-plan supervise serve` runs for a registered job
- **THEN** it performs the boot-scan reconciliation and periodic watchdog ticks
  as part of the service host

#### Scenario: Legacy runs are unaffected

- **WHEN** an operator runs any existing `opsx-plan` command for a plan with no
  registered supervised job
- **THEN** its behavior is unchanged by the watchdog surface

### Requirement: Operator documentation describes the watchdog and reconstitution behavior

The operator-facing `opsx-plan` documentation SHALL describe the watchdog: the
service-owned loop with no control-channel dependency, the separate liveness,
progress, and deadline signals, the job classification vocabulary, boot-scan
reconciliation with reconnect before respawn, quiescence-gated reconstitution,
the restart backoff bound, the no-action rule for an expected human wait, and
the read-only reconstitution-event surface.

#### Scenario: The watchdog surface is documented

- **WHEN** an operator reads the documented `opsx-plan supervise` reference
- **THEN** it covers the watchdog loop, the signals and classifications, boot
  reconciliation, quiescence-gated reconstitution, restart bounds, human-wait
  handling, and the reconstitution-event surface

### Requirement: `opsx-plan autopilot` drives a plan unattended

The orchestrator SHALL provide an `opsx-plan autopilot` subcommand that repeatedly invokes `opsx-plan run` for a resolved plan and reacts to the resulting plan state, so a plan can be driven without a foreground operator at a terminal.

`autopilot` SHALL accept `--plan <path>`, `--veto-window-minutes <N>`, `--max-auto-resets <N>`, `--reset-spacing-seconds <N>`, `--poll-seconds <N>`, and `--once`. When `--plan` is omitted, plan resolution SHALL use the same precedence as the other operator commands. `--once` SHALL perform a single pass and exit without looping.

Autopilot SHALL NOT edit plan execution state directly: it SHALL drive the engine only through the `opsx-plan` executable.

When the engine pauses a change on a budget limit, autopilot SHALL notify and stop rather than looping indefinitely. Autopilot SHALL also stop with an escalation when the loop makes no forward progress: three consecutive quick passes that leave every change's status unchanged are treated as no forward progress rather than retried forever.

#### Scenario: Autopilot loops the run engine to plan completion

- **WHEN** an operator runs `opsx-plan autopilot` for a plan with pending work and no gate or failure
- **THEN** autopilot invokes `opsx-plan run`, observes the resulting state, and keeps invoking the engine until every change is done or skipped before exiting 0

#### Scenario: Single-pass mode runs once and exits

- **WHEN** an operator runs `opsx-plan autopilot --once`
- **THEN** autopilot performs one pass, handles any gate or failure it reaches, and exits without starting another pass

#### Scenario: A budget pause stops the loop

- **WHEN** the engine pauses a change on a spend or time budget limit
- **THEN** autopilot notifies the configured topic and stops rather than invoking the engine again

#### Scenario: No forward progress escalates

- **WHEN** three consecutive quick passes leave every change's status unchanged
- **THEN** autopilot escalates as no forward progress and exits instead of retrying

### Requirement: Autopilot auto-resets bounded transient worker failures

When an `opsx-plan run` invoked by autopilot leaves a change failed with a transient worker failure — invalid subagent output or a stage timeout — autopilot SHALL reset that change and continue the loop instead of escalating immediately.

Auto-reset SHALL be bounded per failure signature. Autopilot SHALL persist each signature's reset count and last-attempt time in `.opsx-plan/autopilot-state.json`, SHALL NOT exceed `max_auto_resets` resets (default 2) for one signature, and SHALL NOT reset the same signature twice within `reset_spacing_seconds` (default 300). The persisted counts SHALL survive an `opsx-plan reset` and a restart of the autopilot process.

When a transient signature's bound is reached, autopilot SHALL escalate it as `transient_exhausted` and SHALL NOT reset it again.

#### Scenario: Transient failure is reset within the bound

- **GIVEN** a change failed with `subagent_output_invalid` and its signature has fewer than `max_auto_resets` recorded resets
- **WHEN** autopilot handles the failure
- **THEN** it invokes `opsx-plan reset` for that change, records the attempt against the signature, and continues the loop

#### Scenario: Reset counts survive an engine reset

- **GIVEN** autopilot has recorded two resets for a failure signature in `.opsx-plan/autopilot-state.json`
- **WHEN** the change is reset again through `opsx-plan reset` and the autopilot process restarts on the same plan
- **THEN** the recorded resets are still counted and the next transient failure of that signature is bounded by them

#### Scenario: Exhausted transient bound escalates

- **WHEN** a transient failure's signature has already reached `max_auto_resets` recorded resets
- **THEN** autopilot escalates the change as `transient_exhausted` and does not reset it again

### Requirement: Autopilot escalates non-transient failures without retrying

Autopilot SHALL classify a failed change that is not a bounded transient failure as requiring a human and SHALL escalate it instead of resetting it. Escalated classes SHALL include permanent provider failures (billing or quota exhaustion), permission rejections, `finding_recurrence_exceeded`, `max_rounds_reached`, `no_progress`, archive failures, and any unrecognized failure.

#### Scenario: Provider billing exhaustion escalates immediately

- **WHEN** a failed change's reason or stage log names a provider billing or quota exhaustion
- **THEN** autopilot escalates the change as `permanent_provider` and does not reset it

#### Scenario: Review budget exhaustion escalates immediately

- **WHEN** a failed change's `last_result` is `finding_recurrence_exceeded`, `max_rounds_reached`, or `no_progress`
- **THEN** autopilot escalates the change and does not reset it

#### Scenario: Archive failure escalates immediately

- **WHEN** a failed change's archive status is failed or its reason names a post-archive failure
- **THEN** autopilot escalates the change as `archive_failed` and does not reset it

#### Scenario: Unknown failure escalates immediately

- **WHEN** a failed change cannot be classified as a bounded transient or a named needs-human class
- **THEN** autopilot escalates the change as `unknown` and does not reset it

### Requirement: Autopilot gives `pause_before` gates a notification and veto window

When `opsx-plan run` leaves a change awaiting approval, autopilot SHALL notify the configured ntfy topic and give the operator a veto window of `veto_window_minutes` (default 30) before auto-approving the gate.

During the window autopilot SHALL auto-approve only after the window elapses; it SHALL stop and escalate the change as `human_veto` when a veto marker file exists at `.opsx-plan/veto/<change-id>`; and it SHALL continue without auto-approving when an out-of-band `opsx-plan approve` records the approval.

#### Scenario: Gate auto-approves after the veto window

- **WHEN** autopilot reaches an awaiting-approval change and no veto marker or manual approval appears within `veto_window_minutes`
- **THEN** it invokes `opsx-plan approve` for the change and continues the loop

#### Scenario: Veto marker stops the gate

- **WHEN** `.opsx-plan/veto/<change-id>` exists during the change's veto window
- **THEN** autopilot escalates the change as `human_veto` and does not auto-approve it

#### Scenario: Manual approval short-circuits the wait

- **WHEN** an operator runs `opsx-plan approve <change-id>` during the veto window and the approval appears in plan state
- **THEN** autopilot continues the loop without waiting out the remaining window and without auto-approving again

### Requirement: Autopilot escalation records a digest and stays down

On escalation, autopilot SHALL append a digest to `.opsx-plan/escalations.jsonl` recording the plan, change id, failure class, `last_result`, reason, findings loci, attempt count, stage log path, and a suggested action. When an ntfy topic is configured it SHALL send a high-priority push, and it SHALL record the decision in its event log.

An escalation for a change that needs human attention SHALL exit 0, so that a unit configured with `Restart=on-failure` stays down for the operator rather than restart-looping.

An environment-class failure that prevents the loop from operating SHALL exit 2 rather than 0, so it is distinguishable from a human-attention escalation.

#### Scenario: Escalation writes a digest and exits clean

- **WHEN** autopilot escalates a failed change or a vetoed gate
- **THEN** it appends a digest to `.opsx-plan/escalations.jsonl`, sends an ntfy push when a topic is configured, records the decision, and exits 0

#### Scenario: Environment failure exits 2

- **WHEN** autopilot cannot obtain a valid plan status or the engine fails for an environment reason
- **THEN** autopilot records the escalation and exits 2 instead of 0

### Requirement: Autopilot records its decisions in a durable event log

Autopilot SHALL append a structured record of its loop decisions to `.opsx-plan/autopilot-events.jsonl`, including run start and exit, gate waits, vetoes and auto-approvals, auto-resets, escalations, and plan completion.

#### Scenario: Decisions are recorded as structured events

- **WHEN** autopilot runs a loop iteration
- **THEN** `.opsx-plan/autopilot-events.jsonl` gains a JSON record for each run boundary, gate wait, reset, veto, escalation, or completion action autopilot takes

### Requirement: Autopilot configuration resolves file, environment, and CLI overrides

Autopilot SHALL read its optional configuration from `~/.config/opsx-controller/autopilot.toml` with keys `ntfy_topic`, `veto_window_minutes`, `max_auto_resets`, `reset_spacing_seconds`, and `poll_seconds`.

When no source supplies a value, autopilot SHALL use its built-in defaults (`veto_window_minutes` 30, `max_auto_resets` 2, `reset_spacing_seconds` 300, and its built-in poll interval). The `OPSX_AUTOPILOT_NTFY_TOPIC` environment variable SHALL override the file's `ntfy_topic`, and explicit CLI flags SHALL override the config file.

An unreadable or malformed config file SHALL be treated as absent rather than aborting the loop.

#### Scenario: Config file values take effect

- **WHEN** `~/.config/opsx-controller/autopilot.toml` sets `max_auto_resets = 1` and no CLI flag or environment variable overrides it
- **THEN** autopilot bounds transient resets at one per signature

#### Scenario: Environment overrides the notification topic

- **WHEN** the config file sets `ntfy_topic` and `OPSX_AUTOPILOT_NTFY_TOPIC` is also set
- **THEN** autopilot pushes to the environment variable's topic

#### Scenario: CLI flag overrides the config file

- **WHEN** the config file sets `veto_window_minutes` and the operator passes `--veto-window-minutes` on the command line
- **THEN** autopilot uses the CLI value for the veto window

#### Scenario: Malformed config does not abort

- **WHEN** `~/.config/opsx-controller/autopilot.toml` is unreadable or malformed
- **THEN** autopilot proceeds with its defaults and notifications disabled unless a topic is otherwise provided

### Requirement: Autopilot notification failures never stop the run loop

An ntfy push is best-effort. If a push cannot be delivered, autopilot SHALL record the failure and continue; it SHALL NOT fail a stage, abort the loop, or change its exit code because a notification could not be sent.

#### Scenario: Undeliverable push does not stop autopilot

- **WHEN** an ntfy push fails or times out during an escalation or a gate wait
- **THEN** autopilot records the notification failure and continues its normal loop and exit behavior

### Requirement: `opsx-plan reset` requires `--force` to reset a done change

On the unregistered legacy path, `opsx-plan reset` SHALL refuse to reset a change whose status is done unless the operator passes `--force`. The refusal SHALL name the affected done change, state that resetting re-runs implement and review cold against the archived copy, and exit non-zero without changing plan state.

The reset command SHALL accept a `--force` flag that overrides the guard. Non-done changes SHALL reset without `--force`, and the broker-mediated reset path for registered supervised jobs SHALL be unaffected.

#### Scenario: Done change is refused without force

- **WHEN** an operator runs `opsx-plan reset <change-id>` for a change whose status is done and does not pass `--force`
- **THEN** the command exits non-zero, names the done change, explains the re-run consequence, and leaves plan state unchanged

#### Scenario: Force overrides the done guard

- **WHEN** an operator runs `opsx-plan reset --force <change-id>` for a done change on the legacy path
- **THEN** the change is reset to pending

#### Scenario: Non-done changes need no force

- **WHEN** an operator runs `opsx-plan reset <change-id>` for a change that is not done
- **THEN** the change is reset without requiring `--force`

### Requirement: Repository documentation describes the direct-dispatch-only execution model

The repository's operator-facing and reference documentation SHALL describe
direct dispatch as the only plan-run execution model. It SHALL state that a
plan missing any of `implement_invoke`, `review_invoke`, or `archive_invoke`
fails closed at load time with an error naming the required keys. It SHALL NOT
present a legacy drive or nested-controller execution mode as an available
workflow.

#### Scenario: Documentation states the fail-closed requirement

- **WHEN** a reader consults the operator workflow or orchestrator reference
  for how a plan run is dispatched
- **THEN** the documentation states that all three stage invokes are required,
  that a plan missing any of them fails closed at load time, and that there is
  no fallback execution path

#### Scenario: Documentation offers no legacy execution mode

- **WHEN** the repository's live documentation is searched for a legacy drive
  or nested-controller plan-run workflow
- **THEN** no document presents one as available, and any historical mention
  explicitly states that the path was removed

### Requirement: Documentation states adapter support for compile and plan-run

The repository documentation SHALL state which adapters `opsx-plan compile`
supports and that Codex CLI plan-run (`opsx-run` / a full stage-invoke plan) is
unsupported on that adapter. It SHALL NOT instruct operators to enable Codex
CLI execution by hand-writing stage invokes.

#### Scenario: Codex CLI plan-run is documented as unsupported

- **WHEN** a reader checks whether the Codex CLI adapter can drive a plan run
- **THEN** the documentation states that plan compilation and single-change
  `opsx-run` are unsupported for Codex CLI and names the supported adapters

#### Scenario: No hand-written opt-in is taught

- **WHEN** the repository documentation describes Codex CLI plan execution
- **THEN** it does not direct the reader to hand-write stage invokes to enable
  it

### Requirement: Repository documentation teaches no deleted surface or retired key

Live repository documentation SHALL NOT describe a deleted controller surface
(`opsx-drive`, `opsx-author`, `opsx-verify-auto`, `opsx-archive-no-prompt`, or
the nested-controller agent) or the retired `invoke` and `max_attempts`
manifest keys as available. Manifest schema documentation SHALL list only the
current configuration keys.

#### Scenario: Deleted surfaces are absent from live documentation

- **WHEN** the repository's README, `docs/`, `core/`, `skills/`, `plugins/`,
  and root `AGENTS.md` are searched for deleted controller surfaces
- **THEN** no document teaches one as a supported workflow, and any historical
  note is marked as removed or archived

#### Scenario: Retired keys are absent from manifest documentation

- **WHEN** a reader consults a manifest schema table
- **THEN** it lists the current direct-dispatch keys and does not present the
  retired `invoke` or `max_attempts` keys as valid configuration
