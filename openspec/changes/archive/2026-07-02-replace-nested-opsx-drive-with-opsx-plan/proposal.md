## Why

`opsx-plan` currently delegates each ready change to a nested `/opsx-drive` controller loop. In practice that layering makes long plan runs slow, opaque, and prone to hanging after implementation work is already complete, so the plan runner needs to own the phase loop directly.

## What Changes

- Add a plan-level orchestration capability for the OpenCode adapter so `opsx-plan` can drive bounded implement, review, and archive stages directly.
- Teach `opsx-plan` to persist per-change phase state, review fix prompts, round counts, and archive evidence in its own durable plan state instead of relying on nested `/opsx-drive` controller state for plan execution.
- Replace the current OpenCode plan-run invocation contract from one long-lived `/opsx-drive` session to short one-shot invocations of the existing implementer, reviewer, and archiver worker surfaces.
- Keep `/opsx-drive` available as a manual single-change controller path outside plan execution, but remove it from the critical path for `opsx-plan run`.
- Update orchestrator and OpenCode adapter documentation, logs, and tests to reflect the thinner orchestration model.

## Capabilities

### New Capabilities
- `plan-driven-opencode-execution`: Defines direct `opsx-plan` ownership of implement, review, and archive stage orchestration for OpenCode-backed plan runs.

### Modified Capabilities

## Impact

- `orchestrator/opsx-plan.py` state model, dispatch flow, retry policy, and log output
- OpenCode adapter command and support docs describing how plan execution works
- OpenCode phase invocation wiring for implementer, reviewer, and archiver worker runs
- Orchestrator regression coverage for resumed runs, review failures, archive success, and blocked states
