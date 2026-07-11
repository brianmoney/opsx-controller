# plan-git-delivery Specification

## Purpose

Define how `opsx-plan` optionally delivers a whole plan run on a dedicated git branch and, when configured, hands that branch off as a pull request under fail-closed orchestrator control.
## Requirements
### Requirement: Plan git delivery is explicitly configured and default-off

The orchestrator SHALL support an optional `plan.git_delivery` configuration group with these keys:

- `plan.git_delivery.enabled` (boolean): enables plan-level branch delivery when `true`. Default is `false`.
- `plan.git_delivery.branch` (string, optional): explicit branch name for the plan delivery branch.
- `plan.git_delivery.base_ref` (string, optional): explicit git ref from which the delivery branch is created and against which a later pull request is targeted.
- `plan.git_delivery.create_pull_request` (boolean): enables pull-request delivery after successful plan completion when `true`. Default is `false`.

When `plan.git_delivery.enabled` is `false` or the `plan.git_delivery` group is absent, the orchestrator SHALL behave exactly as existing non-branching plans do today.

If `plan.git_delivery.create_pull_request` is `true`, the plan SHALL also have `plan.git_delivery.enabled = true`.

#### Scenario: Existing plans remain unaffected by default

- **WHEN** a plan omits `plan.git_delivery` entirely or leaves `plan.git_delivery.enabled = false`
- **THEN** `opsx-plan` does not require a delivery branch, does not persist branch-delivery state, and does not attempt pull-request delivery

#### Scenario: Optional branch and base ref fields may be unset

- **WHEN** a plan sets `plan.git_delivery.enabled = true` and omits `plan.git_delivery.branch` or `plan.git_delivery.base_ref`
- **THEN** the plan is valid and the orchestrator resolves the unset values at the start of the first delivery-enabled run without treating the omissions as a plan-level validation error

#### Scenario: Pull-request delivery requires branch delivery

- **WHEN** a plan sets `plan.git_delivery.create_pull_request = true` and `plan.git_delivery.enabled = false`
- **THEN** the plan is invalid and the orchestrator refuses to run it until branch delivery is enabled or pull-request delivery is disabled

### Requirement: Plan state records authoritative git delivery identity

When plan git delivery is enabled, `.opsx-plan/<plan-name>.state.json` SHALL persist a `git_delivery` object with these fields:

- `git_delivery.base_ref` (string or `null`): the selected base ref for the plan run.
- `git_delivery.branch_name` (string or `null`): the selected delivery branch for the plan run.
- `git_delivery.delivery_status` (string): one of `"disabled"`, `"branch_pending"`, `"branch_ready"`, `"ready_for_pr"`, or `"pr_opened"`.

The recorded `git_delivery.base_ref` and `git_delivery.branch_name` SHALL be authoritative for any resumed run of the same plan state.

#### Scenario: First delivery-enabled run records branch identity

- **WHEN** a delivery-enabled plan run selects a base ref and delivery branch for the first time
- **THEN** the plan state records those exact values under `git_delivery.base_ref` and `git_delivery.branch_name` and sets `git_delivery.delivery_status` to at least `"branch_pending"` before later delivery progress is recorded

#### Scenario: Disabled delivery records disabled status

- **WHEN** a plan run proceeds without git delivery enabled
- **THEN** the plan state either omits `git_delivery` or records an equivalent disabled state with `git_delivery.delivery_status = "disabled"`

### Requirement: Branch creation requires a clean tracked tree

Before the orchestrator creates or first checks out a plan delivery branch, it SHALL verify that the tracked worktree is clean.

If tracked files are dirty, the orchestrator SHALL fail closed before branch creation, checkout, or state mutation that would claim the branch is ready.

#### Scenario: Dirty tracked tree blocks delivery branch creation

- **WHEN** `plan.git_delivery.enabled = true` and the first run starts with tracked file modifications in the worktree
- **THEN** `opsx-plan` exits with an error indicating that branch delivery requires a clean tracked tree and does not create or record a delivery branch

### Requirement: Resumed runs dispatch only from the recorded delivery branch

Once `git_delivery.branch_name` is recorded for a plan run, the orchestrator SHALL verify before every implement, review, or archive dispatch that the current symbolic `HEAD` is on that exact branch.

If `HEAD` is detached or points to a different branch, the orchestrator SHALL fail closed before dispatch and SHALL report both the expected recorded branch and the actual branch state.

#### Scenario: Resume on the recorded branch proceeds

- **WHEN** a delivery-enabled plan state records `git_delivery.branch_name = "opsx/operator-workflow-upgrades"` and the operator resumes the run with `HEAD` on `opsx/operator-workflow-upgrades`
- **THEN** `opsx-plan` may continue dispatching the next stage for the plan

#### Scenario: Resume on the wrong branch is rejected

- **WHEN** a delivery-enabled plan state records `git_delivery.branch_name = "opsx/operator-workflow-upgrades"` and the operator resumes the run with `HEAD` on `main`
- **THEN** `opsx-plan` exits before any stage dispatch and reports that `opsx/operator-workflow-upgrades` was expected but `main` is currently checked out

### Requirement: Archive verification remains valid on the recorded delivery branch

When plan git delivery is enabled and the resume guard has confirmed `HEAD` is on the recorded delivery branch, archive verification SHALL continue using the existing repository evidence rules for the current `HEAD`, including requiring an `archive(<change>):` commit reachable from `HEAD`.

The orchestrator SHALL NOT require that archive commit to already be reachable from `git_delivery.base_ref` before the plan is considered complete.

#### Scenario: Archive commit exists on delivery branch but not base ref

- **WHEN** a delivery-enabled plan run archives a change and the `archive(<change>):` commit is reachable from the recorded delivery branch `HEAD` but not yet from `git_delivery.base_ref`
- **THEN** archive verification for that change succeeds if the other existing archive evidence also succeeds

### Requirement: Pull-request delivery is completion-gated and orchestrator-owned

If `plan.git_delivery.create_pull_request` is `true`, the orchestrator SHALL treat pull-request creation as eligible only after all enabled plan changes are done, all required archive verification has succeeded, and configured fast checks are green for the completed plan state.

Implement, review, and archive workers SHALL NOT create, checkout, or push git branches and SHALL NOT create pull requests. Branch creation, pushing, and pull-request delivery remain orchestrator responsibilities.

#### Scenario: Completed plan becomes eligible for a pull request

- **WHEN** plan git delivery and pull-request delivery are enabled, every enabled change is done, archive verification evidence is complete, and plan fast checks are green
- **THEN** the orchestrator may proceed to the plan's pull-request creation step and update `git_delivery.delivery_status` to `"ready_for_pr"` or a later delivered state

#### Scenario: Worker output cannot satisfy delivery responsibilities

- **WHEN** an implement, review, or archive worker suggests creating a branch, pushing commits, or opening a pull request
- **THEN** that suggestion is not itself authoritative delivery completion and the orchestrator still owns those git-delivery actions and gates

### Requirement: First delivery-enabled runs create and check out a dedicated plan branch

When `plan.git_delivery.enabled = true`, `opsx-plan run` SHALL establish a dedicated delivery branch before dispatching any implement, review, or archive stage unless the operator has invoked the first-run override defined by this capability.

If `plan.git_delivery.branch` is set, that value SHALL be the delivery branch name. Otherwise the orchestrator SHALL derive the branch name deterministically as `opsx/<plan.name>`.

If `plan.git_delivery.base_ref` is set, that value SHALL be the delivery base ref. Otherwise the orchestrator SHALL resolve the base ref from the current symbolic `HEAD` branch at first run start.

If no delivery branch identity has yet been recorded for the plan state, the orchestrator SHALL create and check out the resolved delivery branch from the resolved base ref before later stage dispatch. It SHALL then persist `git_delivery.base_ref`, `git_delivery.branch_name`, and `git_delivery.delivery_status = "branch_ready"`.

#### Scenario: First run creates an explicitly configured delivery branch

- **GIVEN** a plan with `plan.git_delivery.enabled = true`, `plan.git_delivery.branch = "opsx/custom-delivery"`, and `plan.git_delivery.base_ref = "release/next"`
- **AND** the plan state has no recorded `git_delivery.branch_name`
- **AND** the tracked worktree is clean
- **WHEN** the operator runs `opsx-plan run`
- **THEN** the orchestrator creates and checks out `opsx/custom-delivery` from `release/next` before any stage dispatch
- **AND** the plan state records `git_delivery.base_ref = "release/next"`
- **AND** the plan state records `git_delivery.branch_name = "opsx/custom-delivery"`
- **AND** the plan state records `git_delivery.delivery_status = "branch_ready"`

#### Scenario: First run derives the delivery branch from the plan name

- **GIVEN** a plan with `plan.git_delivery.enabled = true` and `plan.name = "operator-workflow-upgrades"`
- **AND** the plan omits `plan.git_delivery.branch` and `plan.git_delivery.base_ref`
- **AND** the plan state has no recorded `git_delivery.branch_name`
- **AND** the tracked worktree is clean
- **AND** the current symbolic `HEAD` branch is `main`
- **WHEN** the operator runs `opsx-plan run`
- **THEN** the orchestrator creates and checks out `opsx/operator-workflow-upgrades` from `main` before any stage dispatch
- **AND** the plan state records `git_delivery.base_ref = "main"`
- **AND** the plan state records `git_delivery.branch_name = "opsx/operator-workflow-upgrades"`

### Requirement: Recorded delivery branch identity is reused on later runs

Once a plan state records `git_delivery.base_ref` or `git_delivery.branch_name`, later runs SHALL treat those recorded values as authoritative and SHALL NOT recompute branch or base identity from the current repository state or updated plan defaults.

If the recorded delivery branch already exists and is checked out, `opsx-plan run` SHALL proceed using that recorded identity without attempting to create a replacement branch.

#### Scenario: Resume on the recorded branch reuses stored identity

- **GIVEN** a delivery-enabled plan state records `git_delivery.base_ref = "main"` and `git_delivery.branch_name = "opsx/operator-workflow-upgrades"`
- **AND** the current `HEAD` is on `opsx/operator-workflow-upgrades`
- **WHEN** the operator runs `opsx-plan run`
- **THEN** the orchestrator reuses the recorded base and branch identity
- **AND** the orchestrator does not derive a new branch name or base ref from ambient git state before dispatching the next stage

### Requirement: `opsx-plan run --no-branch` is a constrained first-run override

`opsx-plan run --no-branch` SHALL disable branch creation and checkout only when the current plan state has not yet recorded a delivery branch identity.

When this override is used on an unrecorded state, the run SHALL proceed without creating or checking out a delivery branch and SHALL leave branch-delivery state disabled or absent for that run.

If the current plan state already records `git_delivery.branch_name`, `--no-branch` SHALL be rejected before any stage dispatch and SHALL NOT bypass the recorded-branch resume guard.

#### Scenario: `--no-branch` skips first-run delivery setup

- **GIVEN** a plan with `plan.git_delivery.enabled = true`
- **AND** the plan state has no recorded `git_delivery.branch_name`
- **WHEN** the operator runs `opsx-plan run --no-branch`
- **THEN** the orchestrator does not create or check out a delivery branch for that run
- **AND** the run may proceed using the existing non-branching execution behavior

#### Scenario: `--no-branch` cannot bypass a recorded branch contract

- **GIVEN** a delivery-enabled plan state records `git_delivery.branch_name = "opsx/operator-workflow-upgrades"`
- **WHEN** the operator runs `opsx-plan run --no-branch`
- **THEN** `opsx-plan` exits before any stage dispatch
- **AND** the error explains that branch delivery has already been recorded for the plan state and cannot be disabled with `--no-branch`

### Requirement: Pull-request delivery prerequisites are checked at run start

When `plan.git_delivery.enabled = true` and `plan.git_delivery.create_pull_request = true`, the orchestrator SHALL verify at `opsx-plan run` start that GitHub pull-request delivery is possible before dispatching any implement, review, or archive stage.

That run-start verification SHALL confirm that the `gh` CLI is available on `PATH` and that the repository has a usable git remote for pushing the recorded delivery branch and targeting the pull request.

If either prerequisite is missing, the orchestrator SHALL fail closed before stage dispatch with a remediation-oriented error.

#### Scenario: Missing `gh` blocks a PR-enabled run before any stage dispatch

- **GIVEN** a plan with `plan.git_delivery.enabled = true` and `plan.git_delivery.create_pull_request = true`
- **AND** `gh` is not available on `PATH`
- **WHEN** the operator runs `opsx-plan run`
- **THEN** `opsx-plan` exits before any stage dispatch
- **AND** the error explains that GitHub CLI availability is required for configured pull-request delivery

#### Scenario: Missing remote blocks a PR-enabled run before any stage dispatch

- **GIVEN** a plan with `plan.git_delivery.enabled = true` and `plan.git_delivery.create_pull_request = true`
- **AND** the repository has no usable git remote for pushing the recorded delivery branch
- **WHEN** the operator runs `opsx-plan run`
- **THEN** `opsx-plan` exits before any stage dispatch
- **AND** the error explains that a git remote is required for configured pull-request delivery

### Requirement: Completed PR-enabled plans push the recorded branch and open exactly one pull request

When `plan.git_delivery.enabled = true` and `plan.git_delivery.create_pull_request = true`, the orchestrator SHALL attempt pull-request delivery only after all enabled changes are done, archive verification evidence is complete, and configured fast checks are green.

At that point, the orchestrator SHALL push the recorded `git_delivery.branch_name` and create a pull request from that branch to the recorded `git_delivery.base_ref` using `gh`.

If the plan state already records a previously opened pull request for the same completed plan run, the orchestrator SHALL treat that recorded PR as authoritative and SHALL NOT create a duplicate pull request on rerun.

If the branch push or pull-request creation fails, the orchestrator SHALL fail closed, SHALL NOT claim PR delivery succeeded, and SHALL leave the plan state reflecting that the pull request is not yet opened.

#### Scenario: Completed plan opens one pull request from the recorded branch

- **GIVEN** a plan state records `git_delivery.base_ref = "main"` and `git_delivery.branch_name = "opsx/operator-workflow-upgrades"`
- **AND** `plan.git_delivery.create_pull_request = true`
- **AND** every enabled change is done
- **AND** archive verification evidence is complete
- **AND** configured fast checks are green
- **WHEN** the orchestrator completes the run
- **THEN** it pushes `opsx/operator-workflow-upgrades`
- **AND** it creates exactly one pull request from `opsx/operator-workflow-upgrades` to `main`

#### Scenario: Rerun after PR creation does not open a duplicate pull request

- **GIVEN** a completed plan state records `git_delivery.delivery_status = "pr_opened"`
- **AND** the plan state records a previously opened pull-request URL for the recorded delivery branch
- **WHEN** the operator reruns `opsx-plan run`
- **THEN** the orchestrator does not create a second pull request
- **AND** the existing recorded pull request remains the authoritative delivery result

#### Scenario: Push or PR creation failure keeps delivery state unambiguous

- **GIVEN** a completed plan is eligible for pull-request delivery
- **AND** pushing the recorded delivery branch or `gh pr create` fails
- **WHEN** the orchestrator attempts pull-request delivery
- **THEN** `opsx-plan` exits with a delivery error
- **AND** the plan state does not claim `git_delivery.delivery_status = "pr_opened"`

### Requirement: Pull-request body is generated from plan report evidence

When the orchestrator creates a pull request for a completed plan run, it SHALL generate the PR body from the plan's existing report aggregation data rather than from worker-authored free text.

That generated body SHALL include per-change status, round counts, and durations for enabled changes. When estimated cost telemetry is available, the body SHALL also include the plan total and any per-change estimated cost details the report already aggregates.

If estimated cost telemetry is unavailable for some or all enabled changes, the generated body SHALL remain valid and SHALL omit or clearly mark unavailable cost fields instead of inventing values.

#### Scenario: PR body includes aggregated telemetry when available

- **GIVEN** a completed plan report includes per-change status, rounds, durations, and estimated cost data
- **WHEN** the orchestrator creates the pull request
- **THEN** the generated PR body includes those aggregated fields as plan evidence

#### Scenario: PR body remains valid when cost telemetry is absent

- **GIVEN** a completed plan report includes per-change status, rounds, and durations but lacks estimated cost data
- **WHEN** the orchestrator creates the pull request
- **THEN** the generated PR body still includes the available report evidence
- **AND** it does not invent missing cost values

### Requirement: Pull-request delivery outcome is persisted and `--no-pr` can suppress one run

When the orchestrator opens a pull request for a completed plan run, `.opsx-plan/<plan-name>.state.json` SHALL record the resulting pull-request URL under `git_delivery.pull_request_url` and SHALL update `git_delivery.delivery_status` to `"pr_opened"` only after PR creation succeeds.

`opsx-plan run --no-pr` SHALL suppress run-start PR prerequisite checks and completion-time push and pull-request creation for that invocation only. It SHALL NOT disable branch delivery, rewrite plan configuration, or erase a previously recorded pull-request URL.

#### Scenario: Successful PR creation records the PR URL in plan state

- **GIVEN** a completed plan becomes eligible for pull-request delivery
- **WHEN** the orchestrator successfully opens the pull request
- **THEN** the plan state records the opened pull-request URL under `git_delivery.pull_request_url`
- **AND** the plan state records `git_delivery.delivery_status = "pr_opened"`

#### Scenario: `--no-pr` suppresses PR delivery for one invocation only

- **GIVEN** a plan with `plan.git_delivery.enabled = true` and `plan.git_delivery.create_pull_request = true`
- **AND** the plan does not yet record a pull-request URL
- **WHEN** the operator runs `opsx-plan run --no-pr`
- **THEN** the orchestrator does not require `gh` or a git remote for that invocation
- **AND** the run may complete without pushing the branch or opening a pull request
- **AND** a later `opsx-plan run` without `--no-pr` may still perform configured pull-request delivery

