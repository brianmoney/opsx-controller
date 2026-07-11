## ADDED Requirements

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
