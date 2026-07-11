## Why

Long `opsx-plan` runs can finish, block on a human gate, or open a pull request long after the operator has stopped watching the terminal. Phase 3 of the operator workflow upgrades plan calls for a lightweight notification hook so operators can route those attention points into their own local tooling without changing core run outcomes.

This change extends the `plan-operator-cli` capability with optional run-event notifications driven by the orchestrator.

## What Changes

- Add an optional `plan.notify_cmd` setting that names a notification command for a plan.
- Invoke that command with a single JSON event argument when a change becomes done, failed, awaiting approval, awaiting acceptance, when the plan completes, and when a pull request is opened.
- Define a stable payload shape that includes the plan name, event type, timestamp, short summary, and change id when the event is change-specific.
- Require notification failures to be logged without failing a stage, changing plan state transitions, or changing the overall run outcome.
- Add runtime tests for event emission points, payload shape, and failure isolation.

## Capabilities

### New Capabilities

- None.

### Modified Capabilities

- `plan-operator-cli`: Adds an optional plan-configured notification hook for important run events.

## Impact

- Affected specs: `openspec/specs/plan-operator-cli/spec.md`.
- Modified files later: `orchestrator/opsx-plan.py` plan parsing, run-event emission points, JSON payload rendering, and best-effort hook execution/logging.
- Runtime behavior later: plans that set `plan.notify_cmd` can emit deterministic notifications for operator attention points and delivery milestones.
- Test coverage later: orchestrator unit tests for each emitted event, payload contents, and hook-failure isolation.
- No bundled service-specific integrations, no retry queue, and no inbound remote control surface.
