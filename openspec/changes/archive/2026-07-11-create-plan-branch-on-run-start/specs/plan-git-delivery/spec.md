## ADDED Requirements

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
