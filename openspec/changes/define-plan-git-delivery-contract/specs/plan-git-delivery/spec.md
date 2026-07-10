## ADDED Requirements

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
