## ADDED Requirements

### Requirement: Operator documentation covers plan git-delivery configuration and guards

The repository SHALL provide operator-facing documentation for plan git
delivery that explains how branch delivery and pull-request delivery are
configured and supervised.

That documentation SHALL cover, at minimum:

- `plan.git_delivery.enabled`, `branch`, `base_ref`, and
  `create_pull_request`
- the default-off behavior for branch and pull-request delivery
- the clean-tracked-tree requirement for first branch creation
- the fail-closed resume guard that requires `HEAD` to match the recorded
  delivery branch
- the conditions under which pull-request delivery occurs

#### Scenario: Operator docs explain delivery setup and fail-closed rules

- **WHEN** an operator reads the plan git-delivery documentation before running
  a delivery-enabled plan
- **THEN** the documentation explains the config keys, default-off behavior,
  branch-creation preconditions, wrong-branch refusal, and PR completion gate

### Requirement: Operator documentation identifies delivery overrides and completion handoff

The same operator documentation SHALL document the invocation-scoped overrides
for delivery behavior and SHALL include an end-to-end example that reaches the
pull-request handoff.

At minimum, the documentation SHALL explicitly describe `--no-branch`,
`--no-pr`, and the point in the workflow where a successful run pushes the
recorded branch and opens a pull request when configured.

#### Scenario: Operator docs explain one-run delivery overrides

- **WHEN** an operator wants to suppress delivery behavior for a single run or
  understand how a completed plan becomes a pull request
- **THEN** the documentation names `--no-branch`, `--no-pr`, and the
  completion-time branch-and-PR handoff behavior
