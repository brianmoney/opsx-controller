## ADDED Requirements

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
