## ADDED Requirements

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
