## 1. Plan State And Invocation Surfaces

- [x] 1.1 Extend `orchestrator/opsx-plan.py` state records so each change can persist direct-execution phase, round, latest fix prompt, review result, archive result, and last-stage log data inside `.opsx-plan/<plan>.state.json`.
- [x] 1.2 Add OpenCode direct-stage invocation defaults and any required manifest/config plumbing so `opsx-plan` can invoke implement, review, and archive workers without calling `/opsx-drive`.

## 2. Direct Stage Dispatch

- [x] 2.1 Implement a bounded stage runner that builds the worker input block for the active change and invokes the OpenCode implementer, reviewer, and archiver as one-shot subprocesses.
- [x] 2.2 Parse machine-readable worker results and persist the corresponding implement, review, and archive state transitions in `opsx-plan`.
- [x] 2.3 Replace the current OpenCode `/opsx-drive` dispatch path in `opsx-plan run` with the new direct stage loop while keeping manual `/opsx-drive` usage outside plan runs untouched.

## 3. Retry, Recovery, And Verification

- [x] 3.1 Move review-failure retry handling, round advancement, and no-progress stop conditions into `opsx-plan` for direct OpenCode execution.
- [x] 3.2 Update reconcile/resume behavior so interrupted OpenCode plan runs recover from plan-owned state instead of requiring `.opencode/opsx-controller/<change>.json`.
- [x] 3.3 Preserve evidence-driven completion rules so archive success still requires worker evidence, archive directory evidence, archive commit evidence, and passing fast checks.

## 4. Documentation And Compatibility

- [x] 4.1 Update `orchestrator/README.md` to describe direct OpenCode stage orchestration, direct state ownership, and the new retry/recovery model.
- [x] 4.2 Update OpenCode adapter command/support documentation to explain that `opsx-plan` no longer uses `/opsx-drive` internally while `/opsx-drive <change-id>` remains the manual single-change controller path.

## 5. Regression Coverage

- [x] 5.1 Add unit tests for direct OpenCode implement, review, and archive dispatch, including JSON parsing and per-stage log/state persistence.
- [x] 5.2 Add recovery tests for interrupted review resumes, review-failure reimplement loops, retry-budget exhaustion, and no-progress stops.
- [x] 5.3 Add verification tests proving archive worker claims do not complete a change unless repository archive evidence and post-archive fast checks also pass.
