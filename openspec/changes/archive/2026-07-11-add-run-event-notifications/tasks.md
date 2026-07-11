## 1. Plan Configuration And Payload Contract

- [x] 1.1 Add an optional `plan.notify_cmd` setting for resolved plans.
- [x] 1.2 Define the JSON notification payload fields for plan name, event type, timestamp, short summary, and optional change id.
- [x] 1.3 Define how notification delivery relates to existing pull-request delivery state so the `pull_request_opened` event has stable source data.

## 2. Event Emission Rules

- [x] 2.1 Emit a notification when a change transitions to done.
- [x] 2.2 Emit a notification when a change transitions to failed.
- [x] 2.3 Emit a notification when a change transitions to awaiting approval or awaiting acceptance.
- [x] 2.4 Emit a notification when the whole plan completes.
- [x] 2.5 Emit a notification when pull-request delivery opens a pull request.
- [x] 2.6 Ensure each listed event is emitted exactly once per transition point.

## 3. Failure Isolation

- [x] 3.1 Run `notify_cmd` as a best-effort side effect that never changes stage verdicts, plan state transitions, or overall run exit semantics.
- [x] 3.2 Log notification command failures with enough detail for operator triage.
- [x] 3.3 Preserve existing behavior for plans that omit `notify_cmd`.

## 4. Verification

- [x] 4.1 Add unit tests for done, failed, awaiting approval, awaiting acceptance, plan complete, and pull-request-opened emission points.
- [x] 4.2 Add unit tests for payload shape, including omission of `change_id` on plan-wide events.
- [x] 4.3 Add unit tests proving a crashing notification hook cannot fail a stage or the run.
- [x] 4.4 Run `python3 -m unittest tests/orchestrator/test_opsx_plan.py`.
- [x] 4.5 Run `openspec validate add-run-event-notifications --strict`.
- [x] 4.6 Re-run `bash adapters/opencode/install.sh --global --verify` because the orchestrator install target changes.
